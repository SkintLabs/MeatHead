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

# Structural patterns that AIs love but humans don't use
BANNED_PATTERNS = [
    r"—",             # Em-dash
    r"--",                 # Double hyphen (em-dash substitute)
    r"^\d+\.\s+",         # Numbered lists (e.g., "1. ")
    r"In conclusion",
    r"Hope this helps",
    r"Here's why",
    r"Let me explain",
    r"Great question",
]


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
            "You are writing as the person described in the knowledge base below. "
            "Read it carefully — it defines your voice, your products, what you can and can't claim, "
            "and how to handle specific situations.\n\n"
        )

        if knowledge:
            base += f"--- KNOWLEDGE BASE ---\n{knowledge}\n--- END KNOWLEDGE BASE ---\n\n"

        base += (
            "HARD FORMATTING RULES (never break these):\n"
            "- Use ONLY standard keyboard characters. No em-dashes. Use commas or full stops instead.\n"
            "- Write in natural paragraphs. No bullet points, no numbered lists.\n"
            "- Don't summarise your own points at the end.\n"
            "- Keep it under 200 words unless the topic genuinely demands more.\n"
            "- Don't start with 'I' as the first word.\n"
        )

        if platform == "reddit":
            base += (
                "\nREDDIT:\n"
                "Write like a real Redditor. Match the subreddit's culture and technical level. "
                "If recommending a tool, frame it as personal experience, never a pitch. "
                "Use casual language and contractions.\n"
            )
        elif platform == "facebook":
            base += (
                "\nFACEBOOK:\n"
                "Engaging and shareable, but still authentic. "
                "Slightly more polished than Reddit. Soft call-to-action only if it fits naturally. "
                "Under 300 words.\n"
            )
        elif platform == "email":
            base += (
                "\nEMAIL:\n"
                "Professional but personal. Clear value in the first sentence. "
                "3-4 sentences max for the body. End with a specific, low-commitment ask.\n"
            )

        if mention_count > 2:
            base += (
                "\nCRITICAL: You've mentioned Skint Labs products a lot recently. "
                "Acknowledge it naturally. Example: 'I know I keep coming back to this, "
                "but WonderwallAi genuinely stopped the breaches we were seeing...'\n"
            )

        return base

    def _validate_output(self, text: str) -> tuple[bool, str]:
        """Check text for AI structural fingerprints. Returns (is_valid, reason_if_invalid)."""
        for pattern in BANNED_PATTERNS:
            if re.search(pattern, text, re.MULTILINE):
                return False, f"Used banned structural pattern: {pattern}"
        return True, ""

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
            {"role": "user", "content": f"Context/Original Post:\n{context}\n\nDraft a reply."},
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
                    f"Validation failed: {reason}. "
                    "Rewrite the response, explicitly avoiding that error. "
                    "Do not apologize, just provide the fixed text."
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
        context = f"Product: {product}\nTopic: {topic}\nTone: {tone}"

        if platform == "email":
            context += "\n\nGenerate a subject line on the first line, then the email body."

        raw_body = await self.generate_reply(platform, context)

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
