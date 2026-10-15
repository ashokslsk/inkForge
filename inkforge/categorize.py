"""Offline book categorisation from title, subjects, description and opening text."""
from __future__ import annotations

import re

from .textclean import KANNADA_RE

# category -> weighted keywords (lower-case; matched as whole words / phrases)
CATEGORIES: list[tuple[str, dict[str, int]]] = [
    ("AI & Machine Learning", {"artificial intelligence": 5, "machine learning": 5, "deep learning": 5, "neural network": 4,
                               "llm": 4, "large language model": 5, "generative ai": 5, "gen ai": 4, "chatgpt": 4, "agentic": 4,
                               "agents": 2, "ai": 3, "reinforcement learning": 5, "data science": 3, "nlp": 3, "transformer": 2,
                               "prompt": 2, "rag": 2, "computer vision": 4, "unsupervised": 3, "pytorch": 4, "tensorflow": 4,
                               "mlops": 5, "graphrag": 5, "context engineering": 5, "multi-agent": 5, "ai engineering": 6,
                               "ai engineers": 6, "ai development": 6, "math for ai": 6, "data without labels": 5}),
    ("Programming & Software Engineering", {"data structures": 5, "computer science": 4, "software engineer": 5, "patterns": 3, "mlops": 2, "programming": 4, "python": 4, "javascript": 4, "java": 3, "c++": 4, "rust": 3, "golang": 4,
                                "software": 3, "developer": 3, "coding": 3, "code": 2, "web development": 4, "django": 4,
                                "react": 3, "android": 3, "kotlin": 4, "swift": 3, "devops": 4, "kubernetes": 4, "docker": 4,
                                "database": 3, "sql": 3, "algorithms": 3, "api": 2, "cloud": 2, "architecture": 2, "cursor": 2,
                                "blockchain": 3, "solidity": 4, "ethereum": 3, "linux": 3, "engineering manager": 3}),
    ("Math, Science & Engineering", {"calculus": 5, "mathematics": 5, "math": 4, "algebra": 4, "geometry": 4, "graph theory": 5,
                                     "statistics": 3, "physics": 5, "chemistry": 5, "biology": 4, "engineering": 3, "quantum": 4,
                                     "control systems": 5, "science": 2, "astronomy": 4, "evolution": 3, "safety engineering": 4,
                                     "ಭೌತಶಾಸ್ತ್ರ": 5, "ಗಣಿತ": 5, "ವಿಜ್ಞಾನ": 4}),
    ("Communication & Persuasion", {"communication": 5, "persuasion": 5, "storytelling": 5, "public speaking": 5, "negotiation": 4,
                                    "influence": 3, "conversation": 4, "pitch": 3, "manipulation": 4, "dark psychology": 5,
                                    "rhetoric": 4, "sales": 3, "charisma": 4, "speak": 2, "verbal": 3, "body language": 4}),
    ("Leadership, Business & Career", {"leadership": 5, "business": 4, "management": 4, "manager": 3, "career": 4, "startup": 4,
                                       "entrepreneur": 4, "marketing": 4, "strategy": 3, "product management": 5, "productivity": 3,
                                       "side hustle": 4, "money": 2, "investing": 3, "finance": 3, "team": 2, "ceo": 3}),
    ("Economics & Geopolitics", {"economics": 5, "economy": 4, "geopolitics": 5, "politics": 4, "global": 2, "trade": 3,
                                 "capitalism": 4, "inflation": 4, "war": 2, "nation": 2, "policy": 3, "china": 2, "empire": 3}),
    ("AI & Society", {"society": 4, "ethics": 4, "future of": 3, "superintelligence": 5, "religion": 4, "myth": 4,
                      "human-machine": 5, "humanity": 4, "deepmind": 4, "mind": 3, "human": 3, "savior": 5, "destroyer": 5,
                      "own image": 5, "inspired action": 5, "world we want": 5, "side hustle": 4, "power": 2, "quest": 3,
                      "computation": 3, "myths": 4, "synergy": 3}),
    ("Spirituality & Wellness", {"spiritual": 5, "spirituality": 5, "meditation": 5, "yoga": 5, "chakra": 5, "mindfulness": 5,
                                 "buddhism": 5, "reiki": 5, "kundalini": 5, "mantra": 4, "pranayama": 5, "breath": 3, "wellness": 4,
                                 "healing": 3, "soul": 3, "zen": 4, "mudras": 5, "ಧಾರ್ಮಿಕ": 5, "ಧರ್ಮ": 3, "ಮನುಸ್ಮೃತಿ": 5}),
    ("Health & Fitness", {"fitness": 5, "workout": 5, "exercise": 4, "bodybuilding": 5, "nutrition": 5, "diet": 4, "health": 3,
                          "anatomy": 4, "running": 3, "strength": 3, "sleep": 3, "weight loss": 5, "body": 2, "biology": 1}),
    ("Psychology & Self-Help", {"psychology": 4, "self-help": 5, "habits": 4, "habit": 3, "mindset": 4, "happiness": 4,
                                "anxiety": 4, "confidence": 4, "motivation": 4, "emotional": 3, "stress": 3, "success": 2}),
    ("Comedy & Humor", {"comedy": 6, "humor": 6, "humour": 6, "funny": 5, "jokes": 6, "satire": 5, "laugh": 4, "comic": 4,
                        "hilarious": 5, "ಹಾಸ್ಯ": 6}),
    ("Fiction & Literature", {"novel": 5, "fiction": 5, "stories": 4, "story": 2, "tales": 4, "poems": 4, "poetry": 5, "mystery": 4,
                              "thriller": 5, "romance": 5, "fantasy": 5, "detective": 4, "adventures": 3, "literature": 4,
                              "ಕಥೆ": 5, "ಕಾದಂಬರಿ": 6, "ಕವನ": 5, "ಕಾವ್ಯ": 5, "ನಾಟಕ": 5}),
    ("History & Biography", {"history": 5, "biography": 6, "memoir": 6, "autobiography": 6, "ancient": 3, "civilization": 4,
                             "life of": 3, "war": 1, "ಇತಿಹಾಸ": 6, "ಚರಿತ್ರೆ": 6, "ಜೀವನ": 3}),
    ("Art & Design", {"art": 4, "design": 4, "drawing": 5, "painting": 5, "abstract art": 6, "photography": 5, "typography": 5,
                      "ui": 2, "ux": 3, "creative": 3, "color": 2, "architecture": 1}),
    ("Kannada Language & Literature", {"ಕನ್ನಡ": 6, "ವ್ಯಾಕರಣ": 6, "ಕವಿರಾಜಮಾರ್ಗ": 6, "ನಿಘಂಟು": 6, "kannada": 6, "dictionary": 3,
                                       "vyakarana": 6, "ಶಬ್ದ": 3, "ಸಾಹಿತ್ಯ": 5}),
]
DEFAULT = "General"


def _score(text: str, kw: dict[str, int]) -> int:
    s = 0
    for k, w in kw.items():
        if KANNADA_RE.search(k):
            n = text.count(k)
        else:
            n = len(re.findall(r"(?<![a-z])" + re.escape(k) + r"(?![a-z])", text))
        s += w * min(n, 3)
    return s


TECH_AI = re.compile(r"(?i)\b(hands-on|engineer|engineering|python|development|mlops|math|practical|deploy|build|"
                     r"production|architecting|agents?|context|rag|data|models?|learning|cursor)\b")
AI_WORD = re.compile(r"(?i)\b(ai|artificial intelligence|machine|superintelligence|deepmind|computation)\b")


def categorize(title: str, subjects: list[str], description: str = "", sample: str = "", kannada: bool = False) -> str:
    head = " ".join([title] + subjects).lower()
    body = (description + " " + sample[:6000]).lower()
    scores = {name: (_score(head, kw), _score(body, kw)) for name, kw in CATEGORIES}
    # AI books split into technical (ML) and ideas (society) by the title's wording
    if AI_WORD.search(title):
        return "AI & Machine Learning" if TECH_AI.search(title) else "AI & Society"
    ranked = sorted(scores.items(), key=lambda kv: (kv[1][0], kv[1][1]), reverse=True)
    name, (hs, bs) = ranked[0]
    if hs >= 3:
        return name
    # title is not telling: fall back to description + opening text
    name, (hs, bs) = max(scores.items(), key=lambda kv: kv[1][1] + 3 * kv[1][0])
    if bs + 3 * hs < 8:
        return "Kannada Books" if kannada else DEFAULT
    return name
