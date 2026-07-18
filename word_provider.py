import json
import logging

import httpx

logger = logging.getLogger(__name__)


def _build_prompt(used_words: list[str]) -> str:
    used_list = "\n".join(f"- {w}" for w in used_words) if used_words else "None (no words used yet)"

    return f"""You are a Word of the Day generator. Your task is to generate one INTERESTING English word.

CRITICAL RULES:
- The word MUST be a REAL English word found in reputable dictionaries
- It should be INTERESTING — not boring basic vocabulary like "cat", "run", "happy"
- But NOT extremely obscure either — a well-read person might have seen it before
- Think: words that make you go "oh that's a cool word" — sophisticated but usable
- ABSOLUTELY DO NOT use any of these already-used words:
{used_list}
- Aim for words with interesting etymology, history, or usage quirks

For the word, provide:
1. The word itself (the headword)
2. Pronunciation (IPA or phonetic spelling)
3. Part of speech
4. A clear, concise definition
5. Etymology / word history (be specific — dates, languages of origin where possible)
6. 2-3 example sentences showing usage in natural context
7. A fun or surprising fact about the word

Respond ONLY with a valid JSON object — no markdown, no backticks, no other text:
{{
  "word": "...",
  "pronunciation": "...",
  "pos": "...",
  "definition": "...",
  "etymology": "...",
  "examples": ["...", "..."],
  "fun_fact": "..."
}}"""


class WordProvider:
    def __init__(self, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.endpoint = f"{self.base_url}/chat/completions"

    async def generate_word(self, used_words: list[str]) -> dict | None:
        prompt = _build_prompt(used_words)

        for attempt in range(2):
            try:
                async with httpx.AsyncClient(timeout=45) as client:
                    resp = await client.post(
                        self.endpoint,
                        json={
                            "model": self.model,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.9,
                        },
                        headers={"Authorization": f"Bearer {self.api_key}"},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"].strip()

                    # Strip possible markdown code fence
                    if content.startswith("```"):
                        content = content.split("\n", 1)[-1]
                        content = content.rsplit("```", 1)[0].strip()

                    result = json.loads(content)

                    # Validate required fields
                    word = result.get("word", "").strip().lower()
                    if not word:
                        logger.warning("Empty word in response, retrying...")
                        continue
                    if word in [w.lower() for w in used_words]:
                        logger.warning("Duplicate word '%s' generated, retrying...", word)
                        continue

                    return result

            except Exception as e:
                logger.warning("Attempt %d failed: %s", attempt + 1, e)

        logger.error("All attempts to generate word failed")
        return None