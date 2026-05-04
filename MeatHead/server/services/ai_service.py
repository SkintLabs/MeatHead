"""
MeatHead — AI Content Generation Engine
Generates humanized marketing content using Groq/Llama 3.3.
Reads a knowledge base (knowledge.md) on startup — or on every call when
RELOAD_KNOWLEDGE=true — so you can update voice/product context without
redeploying.
"""

import re
import logging
import pathlib
from typing import Optional

from groq import AsyncGroq

from server.utils.humanizer import apply_human_entropy

logger = logging.getLogger("meathead.ai")

KNOWLEDGE_PATH = pathlib.Path(__file__).parent.parent.parent / "knowledge.md"

# Structural patterns that indicate AI-generated output
BANNED_PATTERNS = [
    (r"—", "em-dash"),
    (r"--", "double hyphen"),
    (r"^\d+\.\s+", "numbered list"),
    (r"\bIn conclusion\b", "'In conclusion'"),
    (r"\bHope this helps\b", "'Hope this helps'"),
    (r"\bLet me explain\b", "'Let me explain'"),
    (r"\bGreat question\b", "'Great question'"),
    # CTA closers the knowledge base explicitly forbids
    (r"\bCheck (it|them|this) out\b", "CTA 'Check it out'"),
    (r"\bLearn more\b", "CTA 'Learn more'"),
    (r"\bLet me know your thoughts\b", "CTA 'Let me know your thoughts'"),
    (r"I('m| am) (excited|thrilled|pleased|happy) to (share|announce)", "excited-to-share opener"),
    # Third-person narration about yourself or your products
    (r"\bAs a developer,\b", "distancing phrase 'As a developer,'"),
]

# Topic keywords -> specific writing guidance pulled from knowledge.md
TOPIC_GUIDANCE = {
    "product hunt": (
        "This is a Product Hunt launch post. Do NOT pitch the product. "
        "Tell the story of why you built it. You were a developer rolling your own guardrails, "
        "you got tired of it, you built the tool. Mention the launch as the reason you're posting, "
        "not the substance. Write in first person as the founder."
    ),
    "competitor": (
        "You're comparing your tool to a competitor. Be specific and fair. "
        "Say what they do well, then where you took a different angle. "
        "Never trash them. The dev community can smell salt."
    ),
    "reddit": (
        "You're replying to a technical question on Reddit. "
        "Answer the question first. If your tool is genuinely the answer, "
        "mention it once, briefly, late in the reply. "
        "If it's not the answer, just answer the question and don't mention it."
    ),
    "founder story": (
        "This is a founder/about post. First-person, no hero narrative. "
        "'I was building X, hit problem Y, couldn't find a tool I liked, so I built one. Here it is.'"
    ),
    "cold email": (
        "Cold email. Use the recipient's name and one specific thing about their company in the first line. "
        "Get to the ask in sentence two. Done by sentence four."
    ),
}


def _load_knowledge() -> str:
    """Read knowledge.md and return its contents, or empty string on failure."""
    try:
        return KNOWLEDGE_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("knowledge.md not found at %s — running without it", KNOWLEDGE_PATH)
        return ""
    except Exception as exc:
        logger.error("Failed to load knowledge.md: %s", exc)
        return ""


def _topic_guidance(topic: str) -> str:
    """Return specific writing guidance for known topic types."""
    t = topic.lower()
    for keyword, guidance in TOPIC_GUIDANCE.items():
        if keyword in t:
            return guidance
    return ""


class MeatHeadEngine:
    """
    Content generation engine with knowledge-base context and anti-AI validation.
    Generates -> validates -> retries if needed -> humanizes.
    """

    def __init__(self):
        from server.config import get_settings
        settings = get_settings()
        self.api_key = settings.groq_api_key
        self.model = settings.groq_model
        self.reload_knowledge = settings.reload_knowledge
        self.configured = bool(self.api_key)

        self._knowledge: str = _load_knowledge()

        if self.configured:
            self.client = AsyncGroq(api_key=self.api_key)
            logger.info("MeatHeadEngine initialized (Groq connected)")
        else:
            self.client = None
            logger.warning("MeatHeadEngine: GROQ_API_KEY not set")

    def _get_knowledge(self) -> str:
        if self.reload_knowledge:
            return _load_knowledge()
        return self._knowledge

    def _build_system_prompt(self, platform: str, mention_count: int = 0) -> str:
        """Build platform-specific system prompt with knowledge-base context."""
        knowledge = self._get_knowledge()

        base = (
            "You are writing as yourself. First person. You built these products. "
            "You use them. You are not a marketer writing about someone else's work.\n\n"
        )

        if knowledge:
            base += f"--- KNOWLEDGE BASE ---\n{knowledge}\n--- END KNOWLEDGE BASE ---\n\n"

        base += (
            "HARD RULES — breaking any of these means the response is rejected and you rewrite:\n"
            "- Write in first person (I, my, we). NEVER refer to yourself or your products "
            "in the third person. Do not narrate from outside. You ARE the person.\n"
            "- No em-dashes. Use commas or full stops instead.\n"
            "- No bullet points, no numbered lists. Natural paragraphs only.\n"
            "- No summary at the end recapping what you just said.\n"
            "- No CTAs: don't end with 'Check it out', 'Learn more', 'Let me know your thoughts', "
            "or any variation. If the post is good, people click.\n"
            "- No opener phrases: not 'I'm excited to share', not 'Thrilled to announce', "
            "not 'As a developer'.\n"
            "- No rhetorical questions to open ('Ever wondered if...?').\n"
            "- Don't start the first word with 'I'.\n"
            "- Under 200 words unless the topic genuinely demands more.\n"
        )

        if platform == "reddit":
            base += (
                "\nREDDIT: Write like a real Redditor. Match the subreddit's culture and technical level. "
                "If recommending your tool, frame it as personal experience, never a pitch. "
                "Use casual language and contractions.\n"
            )
        elif platform == "facebook":
            base += (
                "\nFACEBOOK: Engaging and shareable, still authentic. "
                "Slightly more polished than Reddit. No hard sell. Under 300 words.\n"
            )
        elif platform == "email":
            base += (
                "\nEMAIL: Professional but personal. Clear value in the first sentence. "
                "3-4 sentences max for the body. End with a specific, low-commitment ask.\n"
            )

        if mention_count > 2:
            base += (
                "\nYou've mentioned your products a lot recently. If you mention them again, "
                "acknowledge it naturally. Example: 'I know I keep coming back to this, "
                "but it genuinely stopped the breaches we were seeing...'\n"
            )

        return base

    def _validate_output(self, text: str) -> tuple[bool, str]:
        """Check text for AI structural fingerprints. Returns (is_valid, reason_if_invalid)."""
        for pattern, label in BANNED_PATTERNS:
            if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
                return False, f"Used banned pattern: {label}"
        return True, ""

    def _build_content_prompt(self, platform: str, product: str, topic: str, tone: str) -> str:
        """
        Build a scenario-grounded user prompt for generate_content.
        This is intentionally different from the generic 'Draft a reply' prompt
        used in generate_reply — it anchors the model in first-person and scenario.
        """
        guidance = _topic_guidance(topic)

        lines = [
            f"Write a {platform} post. You're the founder. First person throughout.",
            f"Topic: {topic}",
            f"Product involved: {product}",
            f"Tone: {tone}",
        ]

        if guidance:
            lines.append(f"\nSpecific guidance for this topic:\n{guidance}")

        if platform == "email":
            lines.append("\nPut the subject line on the first line, then a blank line, then the body.")

        lines.append(
            "\nWrite it now. Don't explain what you're about to write. Just write it."
        )

        return "\n".join(lines)

    async def generate_reply(
        self,
        platform: str,
        context: str,
        mention_count: int = 0,
        max_retries: int = 2,
    ) -> str:
        """
        Generate a humanized reply for a specific platform.
        Validates output, retries with feedback if AI fingerprints detected.
        """
        if not self.configured or not self.client:
            raise RuntimeError("MeatHeadEngine not configured (missing GROQ_API_KEY)")

        messages = [
            {"role": "system", "content": self._build_system_prompt(platform, mention_count)},
            {"role": "user", "content": f"Context/Original Post:\n{context}\n\nWrite a reply as yourself."},
        ]

        draft = ""
        for attempt in range(max_retries + 1):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=400,
            )

            draft = response.choices[0].message.content.strip()

            is_valid, reason = self._validate_output(draft)
            if is_valid:
                return apply_human_entropy(draft)

            logger.warning(f"MeatHead validation failed (attempt {attempt + 1}): {reason}")
            messages.append({"role": "assistant", "content": draft})
            messages.append({
                "role": "user",
                "content": (
                    f"Rejected: {reason}. "
                    "Rewrite it fixing exactly that issue. "
                    "Don't apologize or explain, just provide the fixed text."
                ),
            })

        logger.error("MeatHead failed all validation retries. Applying forced manual override.")
        draft = draft.replace("—", ",").replace("--", ",")
        return apply_human_entropy(draft)

    async def generate_content(
        self,
        platform: str,
        product: str,
        topic: str,
        tone: str = "casual",
    ) -> dict:
        """
        Generate platform-specific marketing content.
        Returns {title, body, raw_body} where body has humanizer applied.
        """
        prompt = self._build_content_prompt(platform, product, topic, tone)

        if not self.configured or not self.client:
            raise RuntimeError("MeatHeadEngine not configured (missing GROQ_API_KEY)")

        messages = [
            {"role": "system", "content": self._build_system_prompt(platform)},
            {"role": "user", "content": prompt},
        ]

        draft = ""
        for attempt in range(3):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=400,
            )
            draft = response.choices[0].message.content.strip()

            is_valid, reason = self._validate_output(draft)
            if is_valid:
                break

            logger.warning(f"generate_content validation failed (attempt {attempt + 1}): {reason}")
            messages.append({"role": "assistant", "content": draft})
            messages.append({
                "role": "user",
                "content": (
                    f"Rejected: {reason}. "
                    "Rewrite it fixing exactly that issue. "
                    "Don't apologize or explain, just provide the fixed text."
                ),
            })
        else:
            logger.error("generate_content failed all retries. Applying forced override.")
            draft = draft.replace("—", ",").replace("--", ",")

        raw_body = apply_human_entropy(draft)

        title = None
        body = raw_body
        if platform == "email" and "\n" in raw_body:
            lines = raw_body.split("\n", 1)
            title = lines[0].strip().lstrip("Subject:").strip()
            body = lines[1].strip() if len(lines) > 1 else raw_body

        return {"title": title, "body": body, "raw_body": raw_body}

    async def improve_draft(self, text: str, instruction: str) -> str:
        """Apply a specific editing instruction to existing text."""
        if not self.configured or not self.client:
            raise RuntimeError("MeatHeadEngine not configured")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a pragmatic editor. Apply the user's instruction to the text exactly. "
                    "Do not use em-dashes. Do not add bullet points or numbered lists. "
                    "Return only the improved text, nothing else."
                ),
            },
            {"role": "user", "content": f"Text:\n{text}\n\nInstruction: {instruction}"},
        ]

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.5,
            max_tokens=400,
        )

        improved = response.choices[0].message.content.strip()
        improved = improved.replace("—", ",").replace("--", ",")
        return apply_human_entropy(improved)

    # --- Legacy method for GiLLBoT email sequences (backward compat) ---

    async def personalize_message(
        self,
        template: str,
        lead_context: dict,
        campaign_context: str,
        subject_template: Optional[str] = None,
    ) -> dict[str, str]:
        """Personalize a message template for a specific lead."""
        if not self.configured:
            return {"subject": subject_template or "", "body": template}

        context = (
            f"Campaign context: {campaign_context}\n"
            f"Lead info: {lead_context}\n"
            f"Template to personalize:\n{template}"
        )
        result = await self.generate_reply("email", context)
        return {"subject": subject_template or "", "body": result}
