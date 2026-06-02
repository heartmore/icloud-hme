#!/usr/bin/env python3
"""
iCloud HME — 邮件本地缓存
===========================
一次拉取终身存储，增量更新，去重合并。

存储: results/mail_cache.json
"""

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "results" / "mail_cache.json"


class MailCache:
    """邮件本地缓存"""

    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict = {}
        self._load()

    def _load(self):
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        if CACHE_FILE.exists():
            try:
                self._data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def _save(self):
        with self._lock:
            CACHE_FILE.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

    def _ensure_account(self, acc_id: str):
        if acc_id not in self._data:
            self._data[acc_id] = {
                "last_checked": None,
                "inbox_emails": [],
                "alias_emails": {},
            }

    def get_inbox(self, acc_id: str) -> List[Dict]:
        with self._lock:
            self._ensure_account(acc_id)
            return list(self._data[acc_id].get("inbox_emails", []))

    def set_inbox(self, acc_id: str, emails: List[Dict]):
        with self._lock:
            self._ensure_account(acc_id)
            existing_ids = {e.get("id") for e in self._data[acc_id]["inbox_emails"]}
            new_emails = [e for e in emails if e.get("id") not in existing_ids]
            if new_emails:
                self._data[acc_id]["inbox_emails"].extend(new_emails)
                if len(self._data[acc_id]["inbox_emails"]) > 500:
                    self._data[acc_id]["inbox_emails"] = \
                        self._data[acc_id]["inbox_emails"][-500:]
            self._data[acc_id]["last_checked"] = datetime.now().isoformat()
            self._save()

    def get_alias_mail(self, acc_id: str, alias_email: str) -> List[Dict]:
        with self._lock:
            self._ensure_account(acc_id)
            return list(self._data[acc_id].get("alias_emails", {}).get(alias_email, []))

    def set_alias_mail(self, acc_id: str, alias_email: str, emails: List[Dict]):
        with self._lock:
            self._ensure_account(acc_id)
            if alias_email not in self._data[acc_id]["alias_emails"]:
                self._data[acc_id]["alias_emails"][alias_email] = []
            existing_ids = {e.get("id") for e in self._data[acc_id]["alias_emails"][alias_email]}
            new_emails = [e for e in emails if e.get("id") not in existing_ids]
            if new_emails:
                self._data[acc_id]["alias_emails"][alias_email].extend(new_emails)
            self._save()

    def set_alias_mail_batch(self, acc_id: str, by_alias: Dict[str, List[Dict]]):
        with self._lock:
            self._ensure_account(acc_id)
            for alias, emails in by_alias.items():
                if alias not in self._data[acc_id]["alias_emails"]:
                    self._data[acc_id]["alias_emails"][alias] = []
                existing_ids = {e.get("id") for e in self._data[acc_id]["alias_emails"][alias]}
                new_emails = [e for e in emails if e.get("id") not in existing_ids]
                if new_emails:
                    self._data[acc_id]["alias_emails"][alias].extend(new_emails)
            self._save()

    def get_all_alias_mail(self, acc_id: str) -> Dict[str, List[Dict]]:
        with self._lock:
            self._ensure_account(acc_id)
            return dict(self._data[acc_id].get("alias_emails", {}))

    def last_checked(self, acc_id: str) -> Optional[str]:
        self._ensure_account(acc_id)
        return self._data[acc_id].get("last_checked")

    def cache_age_seconds(self, acc_id: str) -> float:
        lc = self.last_checked(acc_id)
        if not lc:
            return float("inf")
        try:
            dt = datetime.fromisoformat(lc)
            return (datetime.now() - dt).total_seconds()
        except Exception:
            return float("inf")

    def clear_account(self, acc_id: str):
        if acc_id in self._data:
            del self._data[acc_id]
            self._save()

    def clear_all(self):
        self._data = {}
        self._save()

    def get_stats(self, acc_id: str) -> Dict:
        self._ensure_account(acc_id)
        acc = self._data[acc_id]
        inbox_count = len(acc.get("inbox_emails", []))
        alias_count = sum(len(v) for v in acc.get("alias_emails", {}).values())
        return {
            "last_checked": acc.get("last_checked"),
            "cache_age_sec": self.cache_age_seconds(acc_id),
            "inbox_cached": inbox_count,
            "alias_cached": alias_count,
            "alias_count": len(acc.get("alias_emails", {})),
        }


_mail_cache: Optional[MailCache] = None


def get_cache() -> MailCache:
    global _mail_cache
    if _mail_cache is None:
        _mail_cache = MailCache()
    return _mail_cache
