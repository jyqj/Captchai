"""One-shot branch migration: normalize redis-py response boundaries."""

from __future__ import annotations

from pathlib import Path

path = Path("src/assets/indexed_proxy_pool.py")
text = path.read_text(encoding="utf-8")


def replace_once(old: str, new: str, label: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    text = text.replace(old, new, 1)


if "def _redis_text(" not in text:
    replace_once(
        "    return values or None\n\n\ndef _ordered_sitekeys",
        '''    return values or None


def _redis_text(value: Any) -> str:
    """Normalise a decoded or byte Redis scalar into text."""

    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _redis_members(values: Any) -> List[str]:
    """Normalise Redis range responses across redis-py overloads."""

    if values is None:
        return []
    if isinstance(values, (bytes, str)):
        return [_redis_text(values)]

    members: List[str] = []
    for value in values:
        # The redis-py union also includes withscores=True responses even
        # though this scheduler never requests scores from range commands.
        if isinstance(value, (list, tuple)) and value:
            members.append(_redis_text(value[0]))
        else:
            members.append(_redis_text(value))
    return members


def _ordered_sitekeys''',
        "redis normalisers",
    )

replace_once(
    "        for proxy_id, blob in raw.items():\n            try:\n                proxy = self._deserialize(blob)",
    "        for raw_proxy_id, blob in raw.items():\n            proxy_id = _redis_text(raw_proxy_id)\n            try:\n                proxy = self._deserialize(_redis_text(blob))",
    "reconcile scalar normalisation",
)
replace_once(
    "        existing_ids = set(raw)",
    "        existing_ids = {_redis_text(proxy_id) for proxy_id in raw}",
    "reconcile id set",
)
replace_once(
    "            indexed = set(self._sync_redis.zrange(key, 0, -1))",
    "            indexed = set(\n                _redis_members(self._sync_redis.zrange(key, 0, -1))\n            )",
    "reconcile index members",
)

replace_once(
    '''        ids = await self._redis.zrangebyscore(
            self._cooldown_index_key,
            "-inf",
            now,
            start=0,
            num=self._candidate_window,
        )
        if not ids:
            return

        blobs = await self._redis.hmget(self._proxies_key, ids)''',
    '''        raw_ids = await self._redis.zrangebyscore(
            self._cooldown_index_key,
            "-inf",
            now,
            start=0,
            num=self._candidate_window,
        )
        ids = _redis_members(raw_ids)
        if not ids:
            return

        blobs = await self._redis.hmget(self._proxies_key, ids)''',
    "cooldown candidate normalisation",
)

replace_once(
    '''            ids.extend(
                await self._redis.zrevrange(
                    self._sitekey_index_key(sitekey),
                    0,
                    self._candidate_window - 1,
                )
            )''',
    '''            ids.extend(
                _redis_members(
                    await self._redis.zrevrange(
                        self._sitekey_index_key(sitekey),
                        0,
                        self._candidate_window - 1,
                    )
                )
            )''',
    "sitekey candidates",
)
replace_once(
    '''            ids.extend(
                await self._redis.zrevrange(
                    self._active_all_key,
                    0,
                    self._candidate_window - 1,
                )
            )''',
    '''            ids.extend(
                _redis_members(
                    await self._redis.zrevrange(
                        self._active_all_key,
                        0,
                        self._candidate_window - 1,
                    )
                )
            )''',
    "active candidates",
)
replace_once(
    "            for chunk in await pipe.execute():\n                ids.extend(chunk)",
    "            for chunk in await pipe.execute():\n                ids.extend(_redis_members(chunk))",
    "kind candidates",
)
replace_once(
    "        return list(dict.fromkeys(str(proxy_id) for proxy_id in ids))",
    "        return list(dict.fromkeys(ids))",
    "candidate dedupe",
)

replace_once(
    "        sitekeys = await self._redis.zrange(lru_key, 0, -1)",
    "        sitekeys = _redis_members(\n            await self._redis.zrange(lru_key, 0, -1)\n        )",
    "reverse sitekey index",
)
replace_once(
    "                self._sitekey_index_key(str(sitekey)),",
    "                self._sitekey_index_key(sitekey),",
    "sitekey removal",
)

text = text.replace(
    "self._deserialize(blob)",
    "self._deserialize(_redis_text(blob))",
)
replace_once(
    "                evicted = [str(item[0]) for item in raw_evicted]",
    "                evicted = [_redis_text(item[0]) for item in raw_evicted]",
    "zpopmin response",
)

tail_marker = "    common = {\n"
if tail_marker not in text:
    raise RuntimeError("builder common kwargs marker missing")
prefix, _ = text.split(tail_marker, 1)
text = prefix + '''    cooldown_seconds = int(
        getattr(config, "proxy_cooldown", 120)
    )
    max_consecutive_fails = int(
        getattr(config, "proxy_max_consecutive_fails", 3)
    )
    sitekey_limit = int(
        getattr(config, "proxy_sitekey_limit", 128)
    )
    redis_url = getattr(config, "redis_url", None)
    if redis_url:
        return IndexedRedisProxyPool(
            redis_url,
            cooldown_seconds=cooldown_seconds,
            max_consecutive_fails=max_consecutive_fails,
            max_bytes_per_proxy=max_bytes,
            sitekey_limit=sitekey_limit,
            lock_wait_seconds=float(
                getattr(config, "proxy_lock_wait_seconds", 2.0)
            ),
            candidate_window=int(
                getattr(config, "proxy_candidate_window", 32)
            ),
            lease_ttl_seconds=lease_ttl,
        )
    return ManagedProxyPool(
        cooldown_seconds=cooldown_seconds,
        max_consecutive_fails=max_consecutive_fails,
        max_bytes_per_proxy=max_bytes,
        sitekey_limit=sitekey_limit,
        lease_ttl_seconds=lease_ttl,
    )
'''

compile(text, str(path), "exec")
path.write_text(text, encoding="utf-8")
