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

# Hardcoded product facts. Used as a fallback if knowledge.md can't be loaded
# (e.g. missing in a production deploy). knowledge.md, when present, is the
# source of truth and includes this content plus the extended voice/scenarios.
PRODUCT_FACTS = """
WONDERWALLAI - what it actually is:
- An LLM security SDK (drop-in middleware) that sits in front of your model.
- Catches prompt injection, jailbreaks, PII leaks, and unsafe outputs.
- Sub-2ms latency overhead. Open source MIT-licensed core, hosted API option.
- Live at wonderwallai.skintlabs.ai. Built by Skint Labs (Melbourne).
- Audience: developers shipping LLM features who don't want to roll their own guardrails.
- Tone when mentioning: technical, specific, never marketing-speak.

JERRY - what it actually is:
- An AI customer service bot specifically for Shopify stores.
- Handles order status, shipping, returns, product questions in 8 languages.
- Stripe-billed (Starter / Growth / Scale tiers).
- Live at jerry.skintlabs.ai.
- Audience: small-to-mid Shopify merchants drowning in support tickets.
- Tone when mentioning: practical, results-focused (saved hours, ticket volume down).

SKINT LABS - the parent brand:
- Solo-founder Australian indie lab building dev tools and AI infra.
- skintlabs.ai. Tagline: Build the future.
"""

# Structural patterns that indicate AI-generated output
BANNED_PATTERNS = [
    (r"—", "em-dash"),
    (r"--", "double hyphen"),
    (r"^\d+\.\s+", "numbered list"),
    (r"\bIn conclusion\b", "'In conclusion'"),
    (r"\bHope this helps\b", "'Hope this helps'"),
    (r"\bLet me explain\b", "'Let me explain'"),
    (r"\bGreat question\b", "'Great question'"),
    # AI buzzwords used in the wrong place
    (r"\bgame[- ]?changer\b", "'game-changer'"),
    (r"\bAI[- ]powered\b", "'AI-powered' buzzword"),
    (r"\bsimplifies the process of\b", "'simplifies the process of'"),
    (r"\bunique solution\b", "'unique solution'"),
    (r"\bexciting development\b", "'exciting development'"),
    # Generic three-act opener cliches
    (r"\b(always |has |have |had )?kept me up at night\b", "cliche 'kept me up at night'"),
    (r"\bfor (years|ages|so long)\b.*\b(been|always)\b", "career-summary opener"),
    # CTA closers
    (r"\bCheck (it|them|this) out\b", "CTA 'Check it out'"),
    (r"\bLearn more\b", "CTA 'Learn more'"),
    (r"\bLet me know (your thoughts|what you think|how you go|if)\b", "CTA 'Let me know'"),
    (r"\btake a look (at|and)\b", "CTA 'take a look'"),
    (r"\blooking forward to (hearing|your|getting|any)\b", "CTA 'looking forward to'"),
    (r"\bwould love to hear\b", "CTA 'would love to hear'"),
    (r"\bfeel free to\b", "CTA 'feel free to'"),
    (r"\bdon't hesitate\b", "CTA 'don't hesitate'"),
    (r"\breach out\b", "CTA 'reach out'"),
    # Announcement openers
    (r"I('m| am) (excited|thrilled|pleased|happy) to (share|announce|make|introduce)", "excited-to-share opener"),
    (r"\b(Today|This week|This month) (we('re| are)|I('m| am)) (launching|releasing|announcing|sharing)", "announcement opener"),
    (r"\bit'?s a big (milestone|moment|day)\b", "milestone announcement"),
    # Distancing / third-person narration
    (r"\bAs a developer,\b", "distancing phrase 'As a developer,'"),
    (r"\bevery developer I (spoke|speak|talked|talk) to\b", "distancing generalisation"),
    (r"\bwar stories\b", "cliche 'war stories'"),
    # Vague soft openers that say nothing
    (r"^I've been (building|working on|developing) [A-Za-z\- ]+ for (years|ages|a long time)", "vague career opener"),
]

# Few-shot exemplars: pull the most relevant good post into the user prompt
# directly so the model anchors on it. Pair = (platform, product_keyword) -> example.
EXEMPLARS = {
    ("facebook", "wonderwallai"): (
        "Put WonderwallAi on Product Hunt this week. It's a Python SDK that blocks "
        "prompt injection before it reaches your LLM. Four layers: semantic router catches "
        "90% of off-topic abuse with no API call, then a binary classifier for the sophisticated "
        "stuff, then output scanning for leaked API keys and PII. Runs locally, nothing goes to "
        "a third-party. pip install wonderwallai, MIT licensed, free SDK."
    ),
    ("facebook", "jerry"): (
        "Most Shopify support tickets are the same five questions. Where's my order. "
        "What's the return window. Do you have this in medium. Jerry handles those in 8 languages, "
        "connected to live Shopify order data, voice input included with no extra cost or external API. "
        "$49 a month, no revenue share, installs in five minutes."
    ),
    ("reddit", "wonderwallai"): (
        "The model is treating user input as an instruction because there's no boundary between "
        "your system prompt and what the user sends. That's prompt injection. Layer 1 fix is "
        "explicit delimiters and input sanitisation before the call. If you want a drop-in, I built "
        "wonderwallai for this exact problem. Semantic router catches 90% of it before any LLM is "
        "involved. Free SDK, pip installable."
    ),
}


def _pick_exemplar(platform: str, product: str, topic: str) -> str | None:
    """Find the closest matching good-post example to anchor the model on."""
    p = (platform or "").lower()
    haystack = f"{product} {topic}".lower()
    if "wonderwall" in haystack:
        return EXEMPLARS.get((p, "wonderwallai"))
    if "jerry" in haystack:
        return EXEMPLARS.get((p, "jerry"))
    return None

# Topic keywords -> specific writing guidance pulled from knowledge.md
TOPIC_GUIDANCE = {
    "product hunt": (
        "This is a Product Hunt launch post. Rules: "
        "Do NOT open with 'Today we're launching' or any announcement framing. "
        "Do NOT end with 'looking forward to your thoughts', 'let me know what you think', or any CTA. "
        "Do NOT pitch or describe the product's features. "
        "DO tell the story of why you built it in plain first person. "
        "The structure is: specific frustrating moment you hit as a developer, "
        "what you tried that didn't work, what you built instead, one sentence on where it is now. "
        "The launch is the context, not the story. Under 120 words. "
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
        else:
            # Fallback: knowledge.md missing/unreadable. Use the embedded facts
            # so the model still has product context to work with.
            base += f"--- PRODUCT FACTS ---\n{PRODUCT_FACTS}\n--- END PRODUCT FACTS ---\n\n"

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
                "\nFACEBOOK: Write like a real person posting an update, not a brand announcement. "
                "The audience is a mix of friends, followers, and people who know you or follow Skint Labs. "
                "Lead with something real and human, a moment, a frustration, a decision, not a headline. "
                "If it's a launch post, the launch is one sentence near the end, not the opener. "
                "No 'We are thrilled', no 'I am excited to announce', no hashtag spam. "
                "One or two short paragraphs. Conversational. Under 150 words.\n"
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
        Anchors the model with a few-shot exemplar of a known-good post.
        """
        guidance = _topic_guidance(topic)
        exemplar = _pick_exemplar(platform, product, topic)

        lines = [
            f"Write a {platform} post. You're the founder. First person throughout.",
            f"Topic: {topic}",
            f"Product involved: {product}",
            f"Tone: {tone}",
        ]

        if exemplar:
            lines.append(
                "\nHere's an example of a GOOD post in this exact voice and platform. "
                "Match its structure, density, and tone. Do not copy it verbatim, "
                "write a fresh post on your topic that feels the same:\n"
                f"\"\"\"\n{exemplar}\n\"\"\""
            )

        if guidance:
            lines.append(f"\nSpecific guidance for this topic:\n{guidance}")

        if platform == "email":
            lines.append("\nPut the subject line on the first line, then a blank line, then the body.")

        lines.append(
            "\nWrite it now. Don't explain what you're about to write. Just write it. "
            "Lead with a specific fact or moment, never a career-summary opener."
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
        """Apply a specific editing instruction. Uses knowledge + validation."""
        if not self.configured or not self.client:
            raise RuntimeError("MeatHeadEngine not configured")

        knowledge = self._get_knowledge()
        system = (
            "You are the founder editing your own post. First person throughout. "
            "Apply the user's instruction precisely while keeping concrete facts intact. "
            "Replace vague phrases with specifics from the knowledge base.\n\n"
            "HARD RULES (rejection means you rewrite):\n"
            "- No em-dashes, no bullet points, no numbered lists.\n"
            "- No CTA closers ('Check it out', 'Learn more', 'Let me know what you think', "
            "'looking forward to', 'feel free to').\n"
            "- No 'I'm excited to', 'thrilled to', 'game-changer', 'kept me up at night', "
            "'AI-powered', 'unique solution', 'simplifies the process of'.\n"
            "- No closing summary recap.\n"
            "- Return only the improved text, no preamble.\n\n"
        )
        if knowledge:
            system += f"--- KNOWLEDGE BASE ---\n{knowledge}\n--- END KNOWLEDGE BASE ---"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Current draft:\n{text}\n\nInstruction: {instruction}"},
        ]

        improved = ""
        for attempt in range(3):
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.6,
                max_tokens=500,
            )
            improved = response.choices[0].message.content.strip()
            is_valid, reason = self._validate_output(improved)
            if is_valid:
                break
            logger.warning(f"improve_draft validation failed (attempt {attempt + 1}): {reason}")
            messages.append({"role": "assistant", "content": improved})
            messages.append({
                "role": "user",
                "content": f"Rejected: {reason}. Rewrite fixing exactly that, no apology, no preamble.",
            })
        else:
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
