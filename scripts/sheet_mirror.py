#!/usr/bin/env python3
"""An optional spreadsheet mirror of the desk, built for the counter (RELEASE-PLAN-4.15.md 2.8).

Off unless the profile names a sheet (`mirror.kind`, `mirror.id`, `mirror.url`).
Setup creates a spreadsheet in the shop's Google account through the Maton
gateway, with the same token Gmail and Calendar use (verified on the desk's
pod, 9 September 2026), or accepts the URL of a sheet the owner already has
and checks it can read and write it. After a tick that changed anything the
desk rewrites four tabs from the records and the ledger: "Customers" (one row
per customer, newest activity first), "This week" (the next seven days of
meetings), "Price cards" (quote, cost, profit, assumptions; owner-only
figures live here, never on the first tab), and "Facts" (the ledger in
readable words). The sheet is a mirror: nothing is ever read back from it,
and a Google failure never holds up an inquiry.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import estimate_record
import gateway_token
import kolo_safe
import ledger
import owner_questions

BASE_URL = "https://gateway.maton.ai/google-sheets/v4/spreadsheets"
TITLE = "Jewelry Estimate Desk"
TABS = ("Customers", "This week", "Price cards", "Cost sheet", "Facts")
HEADERS = {
    "Customers": ["Customer", "Email", "Piece", "Status", "Next meeting", "Last contact", "Still open", "Gmail thread", "Estimate"],
    "This week": ["When", "Customer", "Piece", "Status", "Estimate"],
    "Price cards": ["Date", "Customer", "Piece", "Quote", "Hard cost", "Profit", "Margin", "Assumptions", "Outcome", "Estimate"],
    # The owner, 9 September 2026: the full cost breakdown, one line per cost line of every price card.
    "Cost sheet": ["Date", "Customer", "Piece", "Line", "Item", "Quantity", "Unit cost", "Line total", "Outcome", "Estimate"],
    "Facts": ["Customer", "Detail", "Value", "Source", "When", "Estimate"],
}
STATE_FILE = "sheet-mirror.json"
TIMEOUT = 30
Opener = Callable[..., Any]
SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]+)")


def configured(profile: dict[str, Any]) -> dict[str, Any] | None:
    block = profile.get("mirror") if isinstance(profile, dict) else None
    if isinstance(block, dict) and block.get("kind") == "google_sheets" and isinstance(block.get("id"), str) and block["id"].strip():
        return block
    return None


def sheet_id_from(text: str) -> str:
    match = SHEET_ID_RE.search(str(text or ""))
    if match:
        return match.group(1)
    value = str(text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise ValueError("give the spreadsheet's URL or its id")


def _call(method: str, url: str, token: str, body: Any = None, opener: Opener | None = None, tries: int = 2) -> Any:
    """One gateway call, tried twice: a single Google hiccup must not leave half the tabs rewritten."""
    last: OSError | None = None
    for attempt in range(max(1, tries)):
        try:
            return _call_once(method, url, token, body, opener)
        except OSError as exc:
            last = exc
            if "returned 4" in str(exc) and "429" not in str(exc):
                break  # a 4xx other than rate limiting will not change on a retry
    raise last if last else OSError("sheets call failed")


def _call_once(method: str, url: str, token: str, body: Any = None, opener: Opener | None = None) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with (opener or urllib.request.urlopen)(request, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise OSError(f"sheets {method} returned {exc.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise OSError(f"sheets {method} failed: {str(exc)[:200]}") from None
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def _tabs(info: dict[str, Any]) -> dict[str, int]:
    out = {}
    for sheet in info.get("sheets") or []:
        props = sheet.get("properties") or {}
        if props.get("title"):
            out[str(props["title"])] = int(props.get("sheetId") or 0)
    return out


def create(token: str, opener: Opener | None = None) -> dict[str, Any]:
    body = {"properties": {"title": TITLE}, "sheets": [{"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}} for t in TABS]}
    info = _call("POST", BASE_URL, token, body, opener)
    if not info.get("spreadsheetId"):
        raise OSError("the gateway created no spreadsheet")
    return info


def ensure_tabs(sheet_id: str, token: str, opener: Opener | None = None) -> dict[str, int]:
    info = _call("GET", f"{BASE_URL}/{sheet_id}?fields=spreadsheetId,properties.title,sheets.properties", token, None, opener)
    tabs = _tabs(info)
    missing = [t for t in TABS if t not in tabs]
    if missing:
        _call("POST", f"{BASE_URL}/{sheet_id}:batchUpdate", token,
              {"requests": [{"addSheet": {"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}}} for t in missing]}, opener)
        info = _call("GET", f"{BASE_URL}/{sheet_id}?fields=sheets.properties", token, None, opener)
        tabs = _tabs(info)
    return tabs


def format_requests(tabs: dict[str, int], with_rules: bool = True) -> list[dict[str, Any]]:
    """Frozen bold header, readable widths, status colours (the colour rules only at setup, so they never pile up)."""
    requests: list[dict[str, Any]] = []
    for name, sheet_id in tabs.items():
        if name not in TABS:
            continue
        columns = len(HEADERS[name])
        requests.append({"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {"frozenRowCount": 1}},
                                                   "fields": "gridProperties.frozenRowCount"}})
        requests.append({"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": columns},
                                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}, "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93}}},
                                        "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
        requests.append({"autoResizeDimensions": {"dimensions": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": columns}}})
        if with_rules and name in ("Customers", "This week"):
            status_column = HEADERS[name].index("Status")
            for words, colour in (("booked", {"red": 0.85, "green": 0.95, "blue": 0.85}), ("estimate sent", {"red": 0.85, "green": 0.95, "blue": 0.85}),
                                  ("waiting", {"red": 1.0, "green": 0.95, "blue": 0.8}), ("closed", {"red": 0.92, "green": 0.92, "blue": 0.92})):
                requests.append({"addConditionalFormatRule": {"rule": {
                    "ranges": [{"sheetId": sheet_id, "startRowIndex": 1, "startColumnIndex": status_column, "endColumnIndex": status_column + 1}],
                    "booleanRule": {"condition": {"type": "TEXT_CONTAINS", "values": [{"userEnteredValue": words}]},
                                    "format": {"backgroundColor": colour}}}, "index": 0}})
    return requests


def apply_format(sheet_id: str, tabs: dict[str, int], token: str, opener: Opener | None = None, with_rules: bool = True) -> None:
    requests = format_requests(tabs, with_rules)
    if requests:
        _call("POST", f"{BASE_URL}/{sheet_id}:batchUpdate", token, {"requests": requests}, opener)


def setup(workspace: Path, url: str | None = None, token: str | None = None, opener: Opener | None = None) -> dict[str, Any]:
    """Create the mirror sheet, or adopt the owner's, verify it, format it, and write the profile."""
    profile_path = Path(workspace) / "estimate-desk" / "shop-profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    token = token or gateway_token.load_token()
    if url:
        sheet_id = sheet_id_from(url)
        info = _call("GET", f"{BASE_URL}/{sheet_id}?fields=spreadsheetId,spreadsheetUrl,properties.title,sheets.properties", token, None, opener)
        sheet_url = str(info.get("spreadsheetUrl") or url)
        title = str((info.get("properties") or {}).get("title") or "")
    else:
        info = create(token, opener)
        sheet_id = str(info["spreadsheetId"])
        sheet_url = str(info.get("spreadsheetUrl") or f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit")
        title = TITLE
    tabs = ensure_tabs(sheet_id, token, opener)
    try:
        apply_format(sheet_id, tabs, token, opener)
    except OSError:
        pass  # formatting is cosmetic; readiness re-applies it
    profile["mirror"] = {"kind": "google_sheets", "id": sheet_id, "url": sheet_url, "title": title}
    profile_path.write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    # The state remembers which tabs exist, so the first push does not check again; a tab added by a later
    # version is created by that version's first push.
    state_path = _state_path(workspace)
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    state_path.write_text(json.dumps({**state, "tabs": list(TABS), "digest": None}, indent=2) + "\n", encoding="utf-8")
    return profile["mirror"]


def _display(recipient: str) -> str:
    return kolo_safe._sender_display(str(recipient or "")) or str(recipient or "")


def _address(recipient: str) -> str:
    match = re.search(r"<([^>]+)>", str(recipient or ""))
    return (match.group(1) if match else str(recipient or "")).strip()


def _status_words(record: dict[str, Any]) -> str:
    status = str(record.get("status") or "")
    booked = record.get("appointment_booked") if isinstance(record.get("appointment_booked"), dict) else None
    if status == "awaiting_specs":
        words = "waiting on details" if record.get("missing_required_fields") else "reading the details"
        if record.get("inventory_inquiry"):
            words = "ready-made inquiry, visit offered"
    elif status == "pending_approval":
        words = f"price card waiting: ${float(record.get('proposed_price') or 0):,.0f}"
    elif status == "estimate_sent":
        words = f"estimate sent ${float(record.get('proposed_price') or 0):,.0f}"
    elif status == "appointment_booked":
        words = "booked"
    elif status == "approved":
        words = "approved"
    elif status in ("dormant", "declined", "manual_review"):
        words = "closed"
    else:
        words = status.replace("_", " ")
    if booked and booked.get("confirmed_start"):
        words += f"; meeting {_when(booked['confirmed_start'])}"
    return words


def _when(value: Any) -> str:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(value or "")
    return moment.strftime("%a %-d %b %-I:%M %p")


def _last_contact(record: dict[str, Any]) -> str:
    stamps = [str(r.get("recorded_at") or "") for r in (record.get("thread_reviews") or []) if isinstance(r, dict)]
    stamps += [str(record.get("created_at") or ""), str(record.get("updated_at") or "")]
    latest = max((s for s in stamps if s), default="")
    return latest[:16].replace("T", " ")


def _open_words(record: dict[str, Any]) -> str:
    missing = [str(f).split(".")[-1].replace("_", " ") for f in (record.get("missing_required_fields") or [])]
    return ", ".join(dict.fromkeys(missing))


def _thread_link(record: dict[str, Any]) -> str:
    thread_id = (record.get("route") or {}).get("thread_id")
    return f"https://mail.google.com/mail/#all/{thread_id}" if thread_id else ""


def _records(workspace: Path) -> list[dict[str, Any]]:
    root = Path(workspace) / "estimate-desk" / "records"
    found = []
    for path in sorted(root.glob("jed-*.json")) if root.exists() else []:
        try:
            found.append(estimate_record.read_object(path))
        except (OSError, ValueError):
            continue
    return found


# Records the counter never needs: mail that turned out not to be a jewelry inquiry, tests, mistakes, duplicates.
NEVER_LISTED = {"not_an_inquiry", "test_artifact", "created_in_error", "duplicate_of_another_thread", "superseded_by_another_estimate"}


def listed(record: dict[str, Any]) -> bool:
    """Whether a record belongs on the sheet: a customer conversation, not a vendor email that opened and closed a record.

    Live (9 September 2026): every message opens a record before it is read,
    and the ones triage closed as not an inquiry were listed as "closed"
    customers. A record retired for one of those reasons is skipped; a
    customer who withdrew stays, since they were a customer.
    """
    retirement = record.get("retirement") if isinstance(record.get("retirement"), dict) else {}
    return str(retirement.get("reason") or "") not in NEVER_LISTED


def rows_for(workspace: Path) -> dict[str, list[list[Any]]]:
    """Every tab's rows, header first, from the records and the ledger; non-inquiries never appear."""
    desk = Path(workspace) / "estimate-desk"
    records = [r for r in _records(workspace) if listed(r)]
    records.sort(key=_last_contact, reverse=True)
    customers: list[list[Any]] = [HEADERS["Customers"]]
    week: list[list[Any]] = [HEADERS["This week"]]
    cards: list[list[Any]] = [HEADERS["Price cards"]]
    costs: list[list[Any]] = [HEADERS["Cost sheet"]]
    facts: list[list[Any]] = [HEADERS["Facts"]]
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=7)
    for record in records:
        route = record.get("route") or {}
        recipient = str(route.get("recipient") or "")
        name, email = _display(recipient), _address(recipient)
        spec = record.get("specification") or {}
        piece = owner_questions.summary_of_piece(spec) if spec else "their piece"
        booked = record.get("appointment_booked") if isinstance(record.get("appointment_booked"), dict) else None
        next_meeting = _when(booked["confirmed_start"]) if booked and booked.get("confirmed_start") else ""
        customers.append([name, email, piece, _status_words(record), next_meeting, _last_contact(record), _open_words(record),
                          _thread_link(record), record.get("estimate_id", "")])
        if booked and booked.get("confirmed_start"):
            try:
                start = datetime.fromisoformat(str(booked["confirmed_start"]).replace("Z", "+00:00"))
                if start.tzinfo is None:
                    start = start.replace(tzinfo=timezone.utc)
                if now - timedelta(hours=12) <= start <= horizon:
                    week.append([_when(start), name, piece, _status_words(record), record.get("estimate_id", "")])
            except ValueError:
                pass
        for sent in [*(record.get("estimate_history") or []), record]:
            if not isinstance(sent, dict) or sent.get("proposed_price") in (None, ""):
                continue
            sheet = sent.get("internal_cost_sheet") if isinstance(sent.get("internal_cost_sheet"), dict) else {}
            price = float(sent.get("proposed_price") or 0)
            hard = float(sheet.get("hard_cost_total") or 0)
            profit = price - hard if price and hard else ""
            margin = f"{(price - hard) / price * 100:.0f}%" if price and hard else ""
            review = dict(sheet) if sheet else {}
            assumptions = (kolo_safe._assumptions(review) + kolo_safe._choices(sent.get("specification") or spec)).lstrip(". ")
            outcome = "sent" if sent is not record or record.get("status") in ("estimate_sent", "appointment_booked", "approved") else _status_words(record)
            delivery = sent.get("estimate_delivery") if isinstance(sent.get("estimate_delivery"), dict) else {}
            stamp = str(delivery.get("sent_at") or sent.get("approval_requested_at") or sent.get("updated_at") or "")
            card_piece = owner_questions.summary_of_piece(sent.get("specification") or spec) if (sent.get("specification") or spec) else piece
            cards.append([stamp[:16].replace("T", " "), name, card_piece,
                          f"${price:,.2f}", f"${hard:,.2f}" if hard else "", f"${profit:,.2f}" if profit != "" else "", margin, assumptions[:400], outcome,
                          record.get("estimate_id", "")])
            for line in cost_lines(sheet, price):
                costs.append([stamp[:16].replace("T", " "), name, card_piece, *line, outcome, record.get("estimate_id", "")])
        for row in ledger.rows(desk, str(record.get("estimate_id") or "")):
            if row.get("value") is None:
                continue
            field = str(row["field"]).replace("_", " ")
            if row.get("piece") is not None:
                field = f"{estimate_record.piece_label(spec, int(row['piece']))}: {field}" if spec else f"piece {row['piece']}: {field}"
            facts.append([name, field, str(row["value"]), ledger.describe_source(row), str(row.get("at") or "")[:16].replace("T", " "),
                          record.get("estimate_id", "")])
    cards[1:] = sorted(cards[1:], key=lambda r: r[0], reverse=True)
    costs[1:] = sorted(costs[1:], key=lambda r: (r[0], r[9]), reverse=True)
    return {"Customers": customers, "This week": week, "Price cards": cards, "Cost sheet": costs, "Facts": facts}


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return ""


def cost_lines(sheet: dict[str, Any], price: float) -> list[list[Any]]:
    """[Line, Item, Quantity, Unit cost, Line total] for every cost line, then the hard cost total and the quote."""
    out: list[list[Any]] = []
    for line in sheet.get("metal_lines") or []:
        if isinstance(line, dict):
            grams, unit = float(line.get("quantity_grams") or 0), float(line.get("unit_cost") or 0)
            out.append(["metal", str(line.get("metal") or ""), f"{grams:g} g", f"{_money(unit)}/g", _money(grams * unit)])
    for line in sheet.get("stone_lines") or []:
        if isinstance(line, dict):
            carats, unit = float(line.get("quantity") or 0), float(line.get("unit_cost") or 0)
            out.append(["stones", str(line.get("stone") or ""), f"{carats:g} ct", f"{_money(unit)}/ct", _money(carats * unit)])
    for line in sheet.get("labor_lines") or []:
        if isinstance(line, dict):
            hours, rate = float(line.get("hours") or 0), float(line.get("rate") or 0)
            out.append(["labor", str(line.get("task") or ""), f"{hours:g} h", f"{_money(rate)}/h", _money(hours * rate)])
    for line in sheet.get("other_hard_cost_lines") or []:
        if isinstance(line, dict):
            out.append(["fee", str(line.get("label") or ""), "", "", _money(line.get("total_cost"))])
    hard = sheet.get("hard_cost_total")
    if hard not in (None, ""):
        out.append(["hard cost total", "", "", "", _money(hard)])
        try:
            markup = f"{float(price) / float(hard):.2f}x" if float(hard) else ""
        except (TypeError, ValueError):
            markup = ""
        out.append(["quote", f"markup {markup}" if markup else "", "", "", _money(price)])
    return out


def _state_path(workspace: Path) -> Path:
    return Path(workspace) / "estimate-desk" / "run-work" / STATE_FILE


def push(workspace: Path, token: str | None = None, opener: Opener | None = None, force: bool = False) -> dict[str, Any]:
    """Rewrite the four tabs from the desk's state when anything changed; best effort, journaled, never raising."""
    profile_path = Path(workspace) / "estimate-desk" / "shop-profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"pushed": False, "reason": "no profile"}
    mirror = configured(profile)
    if mirror is None:
        return {"pushed": False, "reason": "not configured"}
    state_path = _state_path(workspace)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except (OSError, ValueError):
        state = {}
    try:
        tabs = rows_for(workspace)
    except Exception as exc:  # noqa: BLE001 - the mirror never breaks the desk
        return _journal(state_path, state, {"pushed": False, "reason": f"rows: {str(exc)[:160]}"})
    digest = hashlib.sha256(json.dumps(tabs, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    if not force and state.get("digest") == digest:
        return {"pushed": False, "reason": "unchanged"}
    written: list[str] = []
    try:
        token = token or gateway_token.load_token()
        sheet_id = str(mirror["id"])
        if state.get("tabs") != list(TABS):
            # A tab added since setup (the cost sheet, 9 September 2026) is created on the existing spreadsheet.
            ensure_tabs(sheet_id, token, opener)
            state = {**state, "tabs": list(TABS)}
        # All the tabs in one write, so a Google hiccup never leaves the sheet half new and half old; then one
        # clear of whatever rows lie below the new content.
        data = [{"range": f"'{name}'!A1", "majorDimension": "ROWS",
                 "values": [[str(c) if c is not None else "" for c in row] for row in rows]} for name, rows in tabs.items()]
        _call("POST", f"{BASE_URL}/{sheet_id}/values:batchUpdate", token, {"valueInputOption": "RAW", "data": data}, opener)
        written = list(tabs)
        _call("POST", f"{BASE_URL}/{sheet_id}/values:batchClear", token,
              {"ranges": [f"'{name}'!A{len(rows) + 1}:Z" for name, rows in tabs.items()]}, opener)
    except OSError as exc:
        return _journal(state_path, {**state, "digest": None}, {"pushed": False, "reason": str(exc)[:200], "tabs_written": written,
                                                                 "partial": bool(written) and len(written) < len(tabs)})
    return _journal(state_path, {**state, "digest": digest}, {"pushed": True, "rows": {k: len(v) - 1 for k, v in tabs.items()}})


def _journal(state_path: Path, state: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        state_path.write_text(json.dumps({**state, "last": {**result, "at": datetime.now(timezone.utc).isoformat()}}), encoding="utf-8")
    except OSError:
        pass
    return result


def check(workspace: Path, token: str | None = None, opener: Opener | None = None) -> tuple[str, str]:
    """Readiness: not configured, reachable with its title, or the error; re-applies the formatting when reachable."""
    profile_path = Path(workspace) / "estimate-desk" / "shop-profile.json"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "PASS", "not configured"
    mirror = configured(profile)
    if mirror is None:
        return "PASS", "not configured (optional; sheet_mirror.py setup creates one)"
    try:
        token = token or gateway_token.load_token()
        info = _call("GET", f"{BASE_URL}/{mirror['id']}?fields=properties.title,sheets.properties", token, None, opener)
        tabs = _tabs(info)
        missing = [t for t in TABS if t not in tabs]
        if missing:
            tabs = ensure_tabs(str(mirror["id"]), token, opener)
        try:
            apply_format(str(mirror["id"]), tabs, token, opener, with_rules=False)
        except OSError:
            pass
        return "PASS", f"reachable: {str((info.get('properties') or {}).get('title') or mirror.get('title') or '')} ({mirror.get('url', '')})"
    except (OSError, ValueError) as exc:
        return "WARN", f"unreachable: {str(exc)[:160]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("setup", help="create the mirror sheet, or adopt one by URL, and write the profile")
    s.add_argument("--workspace", type=Path, required=True)
    s.add_argument("--url", default=None, help="the URL of a sheet the owner already has; omitted, a new sheet is created")
    p = sub.add_parser("push", help="rewrite the tabs from the desk's state now")
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--force", action="store_true")
    c = sub.add_parser("check", help="what readiness reports")
    c.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "setup":
            print(json.dumps(setup(args.workspace.resolve(), args.url), sort_keys=True))
        elif args.command == "push":
            print(json.dumps(push(args.workspace.resolve(), force=args.force), sort_keys=True))
        else:
            status, detail = check(args.workspace.resolve())
            print(f"{status} spreadsheet mirror: {detail}")
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
