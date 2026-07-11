# EITElite -- AI Agent Platform
# Copyright (C) 2026 zizetu
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Original repository: https://github.com/zizetu/eite-agent
#

"""
Memory Boot System - EITE Evaluation Initialization
=====================================================

Scans evaluation session memory files and loads them into persistent
storage with category labels. Owned by the EITE evaluation framework
for session state initialization.

Provides:
- boot(): Load all memory files → PersistentMemory + keyword index
- recall(): Keyword-based retrieval (simple TF-IDF weighting)
- update_memory(): Append content to a memory file + PersistentMemory
- get_identity_prompt(): Generate evaluation context prompt from SOUL.md

Author: Tical (Zize Tu)
Version: see tical_code.__version__
"""

# DESIGNED-NOT-DEAD: Boot loader for EITE evaluation session memory files. DO NOT DELETE - needed for cold-start session restoration.

import logging
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# =============================================================================
# Constants
# =============================================================================

MEMORY_FILES = {
    "soul": "Base config/SOUL.md",
    "user": "USER.md",
    "memory": "MEMORY.md",
    "secret": "SECRET.md",
    "tools": "Base config/TOOLS.md",
    "email_rules": "Base config/EMAIL_RULES.md",
}

# Stop words for keyword indexing (English)
_STOP_WORDS: Set[str] = {
    "one", "no", "self", "if", "because", "therefore", "can",
    "this", "that", "what", "how", "why",
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "can", "shall",
    "to", "of", "in", "for", "on", "with", "at", "by", "from",
    "as", "into", "through", "during", "before", "after", "and",
    "but", "or", "not", "no", "if", "then", "than", "so",
}


# =============================================================================
# Tokenizer / TF-IDF utilities
# =============================================================================

def _tokenize(text: str) -> List[str]:
    """Simple tokenizer: split on non-alphanumeric, filter stop words.

    Supports CJK characters and English words.
    """
    tokens = []

    # English words
    english_words = re.findall(r'[a-zA-Z][a-zA-Z0-9_]*', text.lower())
    tokens.extend(w for w in english_words if w not in _STOP_WORDS and len(w) > 1)

    # CJK single-character tokens
    cjk_chars = re.findall(r'[\u4e00-\u9fff]', text)
    tokens.extend(c for c in cjk_chars if c not in _STOP_WORDS)

    # CJK bigrams for improved retrieval precision
    for i in range(len(cjk_chars) - 1):
        bigram = cjk_chars[i] + cjk_chars[i + 1]
        if bigram not in _STOP_WORDS:
            tokens.append(bigram)

    return tokens


def _compute_tf(tokens: List[str]) -> Dict[str, float]:
    """Compute term frequency for a token list.

    Returns:
        Dict mapping token -> TF score
    """
    if not tokens:
        return {}
    counter = Counter(tokens)
    total = len(tokens)
    return {tok: count / total for tok, count in counter.items()}


def _compute_idf(doc_tokens_list: List[List[str]]) -> Dict[str, float]:
    """Compute IDF across a list of documents.

    Returns:
        Dict mapping token -> IDF score
    """
    n_docs = len(doc_tokens_list)
    if n_docs == 0:
        return {}

    doc_freq = Counter()
    for tokens in doc_tokens_list:
        unique = set(tokens)
        for tok in unique:
            doc_freq[tok] += 1

    idf = {}
    for tok, df in doc_freq.items():
        idf[tok] = math.log((n_docs + 1) / (df + 1)) + 1  # smooth IDF

    return idf


# =============================================================================
# Memory Boot Class
# =============================================================================

class MemoryBoot:
    """Evaluation session bootstrapper - load evaluation memory files.

    Used by EITE to initialize evaluation session state from memory files.

    Usage:
        from tical_code.core.memory import PersistentMemory

        pm = PersistentMemory(db_path="~/.EITElite/memory.db")
        boot = MemoryBoot(memory_dir=os.path.expanduser("~/.tical/memory/"), persistent_memory=pm)

        report = await boot.boot()
        results = await boot.recall("evaluation criteria")
        identity = boot.get_identity_prompt()
    """

    def __init__(
        self,
        memory_dir: str,
        persistent_memory=None,
    ):
        """
        Args:
            memory_dir: memory file root directory
            persistent_memory: PersistentMemory instance for evaluation state
        """
        self.memory_dir = os.path.expanduser(memory_dir)
        self.persistent_memory = persistent_memory

        self._memories: Dict[str, str] = {}     # category -> content
        self._tokens: Dict[str, List[str]] = {}  # category -> token list
        self._idf: Dict[str, float] = {}         # token -> IDF score
        self._loaded = False

    # =========================================================================
    # Boot
    # =========================================================================

    async def boot(self) -> Dict:
        """Boot the EITE evaluation memory system - load all memory files.

        EITE evaluation initialization flow:
        1. Scan files in MEMORY_FILES
        2. Read each file content
        3. Store into PersistentMemory (with category tags)
        4. Build TF-IDF index
        5. Return load report

        Returns:
            {"loaded": int, "files": [...], "errors": [...]}
        """
        loaded = 0
        files = []
        errors = []

        for category, rel_path in MEMORY_FILES.items():
            file_path = os.path.join(self.memory_dir, rel_path)
            try:
                if os.path.exists(file_path):
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    self._memories[category] = content
                    self._tokens[category] = _tokenize(content)

                    if self.persistent_memory:
                        self.persistent_memory.store(
                            key=f"memory_boot:{category}",
                            value=content,
                            category=f"memory_file:{category}",
                            priority=self._category_priority(category),
                        )

                    loaded += 1
                    files.append({
                        'category': category,
                        'path': rel_path,
                        'size': len(content),
                        'tokens': len(self._tokens[category]),
                    })
                    logger.info(
                        f"[EITE MemoryBoot] loaded {category}: {rel_path} "
                        f"({len(content)} chars, {len(self._tokens[category])} tokens)"
                    )
                else:
                    logger.debug(f"[EITE MemoryBoot] skip non-existent file: {rel_path}")
                    files.append({
                        'category': category,
                        'path': rel_path,
                        'size': 0,
                        'tokens': 0,
                        'skipped': True,
                    })
            except Exception as e:
                errors.append({
                    'category': category,
                    'path': rel_path,
                    'error': str(e),
                })
                logger.error(f"[EITE MemoryBoot] load failed {category}: {e}")

        if self._tokens:
            all_token_lists = list(self._tokens.values())
            self._idf = _compute_idf(all_token_lists)

        self._loaded = True
        report = {
            'loaded': loaded,
            'total_files': len(MEMORY_FILES),
            'files': files,
            'errors': errors,
        }
        logger.info(
            f"[EITE MemoryBoot] boot complete: {loaded}/{len(MEMORY_FILES)} files loaded, "
            f"{len(errors)} errors, IDF vocabulary {len(self._idf)} tokens"
        )
        return report

    # =========================================================================
    # Recall (retrieve)
    # =========================================================================

    async def recall(self, query: str, top_k: int = 5) -> List[Dict]:
        """Recall relevant evaluation memories using keyword + TF-IDF weighting.

        Args:
            query: query text
            top_k: return top K results

        Returns:
            [{"source": str, "content": str, "relevance": float}, ...]
        """
        if not self._loaded:
            logger.warning("[EITE MemoryBoot] memory not yet booted, please call boot() first")
            return []

        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        scores: List[Tuple[str, float]] = []
        for category, cat_tokens in self._tokens.items():
            if not cat_tokens:
                continue

            tf = _compute_tf(cat_tokens)
            score = 0.0
            for qt in query_tokens:
                if qt in tf:
                    idf = self._idf.get(qt, 1.0)
                    score += tf[qt] * idf

            if cat_tokens:
                score /= math.log(len(cat_tokens) + 1)

            if score > 0:
                scores.append((category, score))

        scores.sort(key=lambda x: x[1], reverse=True)

        results = []
        for category, relevance in scores[:top_k]:
            content = self._memories.get(category, '')
            results.append({
                'source': category,
                'content': content[:500],
                'relevance': round(relevance, 4),
            })

        return results

    # =========================================================================
    # Update Memory
    # =========================================================================

    async def update_memory(self, category: str, content: str) -> bool:
        """Update evaluation memory - append to file and PersistentMemory.

        Args:
            category: memory category (soul/user/memory/secret/tools/email_rules)
            content: new content (append mode)

        Returns:
            True if updated successfully
        """
        if category not in MEMORY_FILES:
            logger.error(f"[EITE MemoryBoot] unknown memory category: {category}")
            return False

        rel_path = MEMORY_FILES[category]
        file_path = os.path.join(self.memory_dir, rel_path)

        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'a', encoding='utf-8') as f:
                f.write(f"\n{content}")

            if category in self._memories:
                self._memories[category] += f"\n{content}"
            else:
                self._memories[category] = content

            self._tokens[category] = _tokenize(self._memories[category])

            all_token_lists = list(self._tokens.values())
            self._idf = _compute_idf(all_token_lists)

            if self.persistent_memory:
                self.persistent_memory.store(
                    key=f"memory_boot:{category}",
                    value=self._memories[category],
                    category=f"memory_file:{category}",
                    priority=self._category_priority(category),
                )

            logger.info(f"[EITE MemoryBoot] updated {category}: appended {len(content)} chars")
            return True

        except Exception as e:
            logger.error(f"[EITE MemoryBoot] update failed {category}: {e}")
            return False

    # =========================================================================
    # Identity Prompt (EITE evaluation context)
    # =========================================================================

    def get_identity_prompt(self) -> str:
        """Generate EITE evaluation context prompt from SOUL.md.

        This prompt is injected into the evaluation system context,
        providing the evaluation framework's identity, scope, and rules.

        Returns:
            Formatted identity prompt string
        """
        soul_content = self._memories.get('soul', '')
        if not soul_content:
            return ""

        prompt = soul_content
        max_identity_length = 2000
        if len(prompt) > max_identity_length:
            prompt = prompt[:max_identity_length] + "\n... [identity setting truncated]"

        return prompt

    # =========================================================================
    # Helper methods
    # =========================================================================

    @staticmethod
    def _category_priority(category: str) -> int:
        """Get priority for a memory category.

        soul/secret have highest priority.
        """
        priorities = {
            'soul': 10,
            'secret': 9,
            'user': 8,
            'memory': 7,
            'tools': 5,
            'email_rules': 5,
        }
        return priorities.get(category, 5)

    def is_loaded(self) -> bool:
        """Check if evaluation memory has been loaded."""
        return self._loaded

    def get_loaded_categories(self) -> List[str]:
        """Get list of loaded categories."""
        return list(self._memories.keys())

    def get_stats(self) -> Dict:
        """Get evaluation memory boot statistics."""
        return {
            'loaded': len(self._memories),
            'categories': list(self._memories.keys()),
            'total_chars': sum(len(c) for c in self._memories.values()),
            'total_tokens': sum(len(t) for t in self._tokens.values()),
            'idf_vocab_size': len(self._idf),
        }


def ensure_memory_files(memory_dir: str) -> None:
    """Bootstrap evaluation session memory files if they don't exist (cold start).

    Creates minimal seed files for SOUL, TOOLS, MEMORY, USER so the
    evaluation system has something to work with on first run.
    """
    import os
    _dir = os.path.expanduser(memory_dir)
    _base = os.path.join(_dir, "Base config")
    os.makedirs(_base, exist_ok=True)

    _seeds = {
        os.path.join(_base, "SOUL.md"): "# SOUL - EITE Evaluation Identity\n\n- **Name**: eite-agent worker\n- **Version**: v0.8.3\n- **Purpose**: Evaluate AI agent performance\n",
        os.path.join(_base, "TOOLS.md"): "# TOOLS - Evaluation Tool Knowledge\n\n- eval_run: execute evaluation\n- eval_report: generate report\n- eval_compare: compare results\n- eval_verify: verify integrity\n",
        os.path.join(_dir, "MEMORY.md"): "# MEMORY - Evaluation History\n\n## Learned\n- Always call end_task when done\n- Track resource usage per session\n- Validate memory integrity after each evaluation\n",
        os.path.join(_dir, "USER.md"): "# USER - Evaluation Preferences\n\n- Framework: EITE (Agent Evaluation Framework)\n- Mode: evaluation\n- Output: structured JSON reports\n",
    }

    _created = 0
    for path, content in _seeds.items():
        if not os.path.exists(path):
            with open(path, 'w') as f:
                f.write(content)
            _created += 1

    if _created:
        import logging
        _log = logging.getLogger(__name__)
        _log.info("[EITE MemoryBoot] Created %d seed files in %s", _created, _dir)
