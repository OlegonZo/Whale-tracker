#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
spc_tx_enricher.py

Обогащение уведомлений Solana-бота:
- полный адрес токен-аккаунта и владельца;
- подпись транзакции;
- адреса отправителя/получателя;
- баланс до/после и изменение;
- тип действия: transfer / swap / add_liquidity / remove_liquidity / unknown;
- ссылки Solscan.

Работает через стандартный Solana JSON-RPC, без стороннего SDK.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

DEFAULT_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

DEX_PROGRAM_HINTS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "whirLbMiicVdio4qvUfM5KAg6CtVwpV9d6nN4GmXK3": "Orca Whirlpool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
}

@dataclass
class TokenBalanceChange:
    account_index: int
    account: Optional[str]
    mint: Optional[str]
    owner: Optional[str]
    decimals: int
    before: Decimal
    after: Decimal

    @property
    def delta(self) -> Decimal:
        return self.after - self.before

@dataclass
class EnrichedTransaction:
    ok: bool
    signature: str
    slot: Optional[int]
    block_time: Optional[int]
    success: Optional[bool]
    fee_sol: Optional[Decimal]
    watched_address: Optional[str]
    token_mint: Optional[str]
    watched_changes: List[Dict[str, Any]]
    all_token_changes: List[Dict[str, Any]]
    senders: List[str]
    recipients: List[str]
    action_type: str
    action_label: str
    dex_programs: List[str]
    account_keys: List[str]
    solscan_tx_url: str
    solscan_account_urls: List[str]
    error: Optional[str] = None

class SolanaRpcError(RuntimeError):
    pass

def _rpc_call(rpc_url: str, method: str, params: list, timeout: int = 25) -> Any:
    response = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise SolanaRpcError(json.dumps(payload["error"], ensure_ascii=False))
    return payload.get("result")

def _account_key_strings(message: Dict[str, Any]) -> List[str]:
    keys = []
    for item in message.get("accountKeys") or []:
        if isinstance(item, str):
            keys.append(item)
        elif isinstance(item, dict):
            keys.append(str(item.get("pubkey") or ""))
        else:
            keys.append(str(item))
    return keys

def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")

def _ui_amount(balance: Dict[str, Any]) -> Decimal:
    ui = balance.get("uiTokenAmount") or {}
    if ui.get("uiAmountString") is not None:
        return _to_decimal(ui["uiAmountString"])
    amount = _to_decimal(ui.get("amount", "0"))
    decimals = int(ui.get("decimals", 0) or 0)
    return amount / (Decimal(10) ** decimals)

def _token_balance_map(balances: Iterable[Dict[str, Any]]) -> Dict[Tuple[int, Optional[str], Optional[str]], Dict[str, Any]]:
    result = {}
    for item in balances or []:
        key = (int(item.get("accountIndex", -1)), item.get("mint"), item.get("owner"))
        result[key] = item
    return result

def _build_token_changes(tx: Dict[str, Any], account_keys: List[str]) -> List[TokenBalanceChange]:
    meta = tx.get("meta") or {}
    pre_map = _token_balance_map(meta.get("preTokenBalances") or [])
    post_map = _token_balance_map(meta.get("postTokenBalances") or [])
    changes = []
    for key in sorted(set(pre_map) | set(post_map), key=lambda x: x[0]):
        pre = pre_map.get(key, {})
        post = post_map.get(key, {})
        account_index = int(post.get("accountIndex", pre.get("accountIndex", -1)))
        mint = post.get("mint") or pre.get("mint")
        owner = post.get("owner") or pre.get("owner")
        ui = post.get("uiTokenAmount") or pre.get("uiTokenAmount") or {}
        decimals = int(ui.get("decimals", 0) or 0)
        account = account_keys[account_index] if 0 <= account_index < len(account_keys) else None
        changes.append(TokenBalanceChange(account_index, account, mint, owner, decimals, _ui_amount(pre), _ui_amount(post)))
    return changes

def _extract_program_ids(tx: Dict[str, Any], account_keys: List[str]) -> List[str]:
    program_ids = []
    message = ((tx.get("transaction") or {}).get("message") or {})
    instructions = list(message.get("instructions") or [])
    for group in (tx.get("meta") or {}).get("innerInstructions") or []:
        instructions.extend(group.get("instructions") or [])
    for ix in instructions:
        if not isinstance(ix, dict):
            continue
        program_id = ix.get("programId")
        if isinstance(program_id, str):
            program_ids.append(program_id)
            continue
        index = ix.get("programIdIndex")
        if isinstance(index, int) and 0 <= index < len(account_keys):
            program_ids.append(account_keys[index])
    return list(dict.fromkeys(program_ids))

def _infer_action(changes: List[TokenBalanceChange], watched_changes: List[TokenBalanceChange], program_ids: List[str]):
    dex_programs = list(dict.fromkeys(DEX_PROGRAM_HINTS[pid] for pid in program_ids if pid in DEX_PROGRAM_HINTS))
    changed_mints = {x.mint for x in changes if x.delta != 0 and x.mint}
    positive_count = sum(1 for x in changes if x.delta > 0)
    negative_count = sum(1 for x in changes if x.delta < 0)
    if dex_programs and len(changed_mints) >= 2:
        return "swap", f"swap через {', '.join(dex_programs)}", dex_programs
    if dex_programs and positive_count >= 2 and negative_count >= 2:
        watched_delta = sum((x.delta for x in watched_changes), Decimal("0"))
        if watched_delta < 0:
            return "add_liquidity", f"возможное добавление ликвидности через {', '.join(dex_programs)}", dex_programs
        if watched_delta > 0:
            return "remove_liquidity", f"возможное снятие ликвидности через {', '.join(dex_programs)}", dex_programs
    if positive_count >= 1 and negative_count >= 1 and len(changed_mints) == 1:
        return "transfer", "перевод SPL-токенов", dex_programs
    return "unknown", "не удалось надёжно классифицировать", dex_programs

def enrich_spc_transaction(signature: str, watched_address: Optional[str] = None, token_mint: Optional[str] = None, rpc_url: str = DEFAULT_RPC_URL) -> Dict[str, Any]:
    solscan_tx_url = f"https://solscan.io/tx/{signature}"
    try:
        tx = _rpc_call(rpc_url, "getTransaction", [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}])
        if tx is None:
            raise SolanaRpcError("Транзакция не найдена или RPC ещё не проиндексировал её.")
        message = ((tx.get("transaction") or {}).get("message") or {})
        account_keys = _account_key_strings(message)
        changes = _build_token_changes(tx, account_keys)
        filtered = [x for x in changes if not token_mint or x.mint == token_mint]
        watched_changes = [x for x in filtered if not watched_address or x.account == watched_address or x.owner == watched_address]
        action_type, action_label, dex_programs = _infer_action(filtered, watched_changes, _extract_program_ids(tx, account_keys))
        senders = list(dict.fromkeys(x.owner or x.account for x in filtered if x.delta < 0 and (x.owner or x.account)))
        recipients = list(dict.fromkeys(x.owner or x.account for x in filtered if x.delta > 0 and (x.owner or x.account)))
        meta = tx.get("meta") or {}
        fee_sol = Decimal(meta["fee"]) / Decimal(1_000_000_000) if meta.get("fee") is not None else None
        related = list(dict.fromkeys([*senders, *recipients, *[x.account for x in watched_changes if x.account], *[x.owner for x in watched_changes if x.owner]]))
        result = EnrichedTransaction(
            ok=True,
            signature=signature,
            slot=tx.get("slot"),
            block_time=tx.get("blockTime"),
            success=meta.get("err") is None,
            fee_sol=fee_sol,
            watched_address=watched_address,
            token_mint=token_mint,
            watched_changes=[{**asdict(x), "before": str(x.before), "after": str(x.after), "delta": str(x.delta)} for x in watched_changes],
            all_token_changes=[{**asdict(x), "before": str(x.before), "after": str(x.after), "delta": str(x.delta)} for x in filtered if x.delta != 0],
            senders=senders,
            recipients=recipients,
            action_type=action_type,
            action_label=action_label,
            dex_programs=dex_programs,
            account_keys=account_keys,
            solscan_tx_url=solscan_tx_url,
            solscan_account_urls=[f"https://solscan.io/account/{address}" for address in related],
        )
        return asdict(result)
    except Exception as exc:
        return asdict(EnrichedTransaction(False, signature, None, None, None, None, watched_address, token_mint, [], [], [], [], "unknown", "ошибка разбора", [], [], solscan_tx_url, [], str(exc)))

def _fmt_amount(value: Any) -> str:
    amount = _to_decimal(value)
    return f"{amount:,.6f}".replace(",", " ").rstrip("0").rstrip(".")

def format_spc_alert(data: Dict[str, Any]) -> str:
    if not data.get("ok"):
        return f"⚠️ НЕ УДАЛОСЬ РАЗОБРАТЬ ТРАНЗАКЦИЮ\n\nПодпись: {data.get('signature')}\nОшибка: {data.get('error')}\nSolscan: {data.get('solscan_tx_url')}"
    changes = data.get("watched_changes") or data.get("all_token_changes") or []
    lines = [
        "🔎 ON-CHAIN ПРОВЕРКА SPC",
        "",
        f"Статус: {'успешно' if data.get('success') else 'ошибка транзакции'}",
        f"Тип действия: {data.get('action_label')}",
        f"Подпись: {data.get('signature')}",
        f"Слот: {data.get('slot')}",
        f"Комиссия: {_fmt_amount(data.get('fee_sol'))} SOL",
        "",
    ]
    if data.get("watched_address"):
        lines.append(f"Наблюдаемый адрес: {data['watched_address']}")
    if data.get("token_mint"):
        lines.append(f"Mint токена: {data['token_mint']}")
    if data.get("senders"):
        lines.extend(["", "Отправители:", *[f"• {x}" for x in data["senders"]]])
    if data.get("recipients"):
        lines.extend(["", "Получатели:", *[f"• {x}" for x in data["recipients"]]])
    lines.extend(["", "Изменения баланса:"])
    for item in changes:
        lines.extend([
            f"• Токен-аккаунт: {item.get('account') or '—'}",
            f"  Владелец: {item.get('owner') or '—'}",
            f"  До: {_fmt_amount(item.get('before'))}",
            f"  После: {_fmt_amount(item.get('after'))}",
            f"  Изменение: {_fmt_amount(item.get('delta'))}",
        ])
    if data.get("dex_programs"):
        lines.extend(["", f"DEX/AMM: {', '.join(data['dex_programs'])}"])
    lines.extend(["", f"Транзакция: {data.get('solscan_tx_url')}"])
    for idx, url in enumerate(data.get("solscan_account_urls") or [], start=1):
        lines.append(f"Адрес {idx}: {url}")
    return "\n".join(lines)

def enrich_and_format(signature: str, watched_address: Optional[str] = None, token_mint: Optional[str] = None, rpc_url: str = DEFAULT_RPC_URL) -> str:
    return format_spc_alert(enrich_spc_transaction(signature, watched_address, token_mint, rpc_url))
