"""Parse SEC Form 4 ownershipDocument XML embedded in full-submission .txt files."""
import re
import xml.etree.ElementTree as ET

_XML_BLOCK = re.compile(r"<XML>(.*?)</XML>", re.DOTALL | re.IGNORECASE)
# Pointer-only placeholder titles ("See Remarks", "See Remarks*", "See remarks below.")
# whose real value lives in the top-level <remarks> element.
_SEE_REMARKS_RE = re.compile(r"^see\s+remarks(\s+below)?\W*$", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


class Form4ParseError(Exception):
    pass


def extract_ownership_xml(submission_text: str) -> str:
    """Pull the ownershipDocument XML out of the SGML-wrapped submission."""
    for m in _XML_BLOCK.finditer(submission_text):
        block = m.group(1).strip()
        if "<ownershipDocument" in block:
            return block
    raise Form4ParseError("no ownershipDocument XML block found")


def _text(el: ET.Element | None) -> str | None:
    if el is None or el.text is None:
        return None
    t = el.text.strip()
    return t or None


def _find_text(root: ET.Element, path: str) -> str | None:
    return _text(root.find(path))


def _value(root: ET.Element, path: str) -> str | None:
    """Read the <value> child of an element; footnote-only elements have none."""
    el = root.find(path)
    if el is None:
        return None
    return _text(el.find("value"))


def _date(s: str | None) -> str | None:
    """Normalize xs:date to YYYY-MM-DD — filers sometimes append a timezone
    offset ("2026-05-29-05:00") that breaks date math downstream."""
    if s is None:
        return None
    m = _DATE_RE.match(s.strip())
    return m.group(1) if m else s


def _num(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _flag(s: str | None) -> int:
    return 1 if s is not None and s.strip().lower() in ("1", "true") else 0


def parse_form4(submission_text: str) -> dict:
    """Parse one Form 4 submission into issuer / owners / transaction lines.

    Returns {'issuer': {...}, 'owners': [...], 'txns': [...],
             'period_of_report': str|None, 'document_type': str|None}.
    Holdings-only rows (nonDerivativeHolding / derivativeHolding) are ignored —
    they report positions, not trades.
    """
    xml = extract_ownership_xml(submission_text)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Form4ParseError(f"XML parse failure: {e}") from e

    issuer = {
        "cik": (_find_text(root, "issuer/issuerCik") or "").lstrip("0") or None,
        "name": _find_text(root, "issuer/issuerName"),
        "ticker": _find_text(root, "issuer/issuerTradingSymbol"),
    }

    remarks = _find_text(root, "remarks")
    # Footnote texts, joined per transaction below — they carry the 10b5-1 /
    # DRIP / offering tells the V2 judgement layer keys on.
    footnotes = {fn.get("id"): _text(fn) for fn in root.findall("footnotes/footnote")}
    owners = []
    for ro in root.findall("reportingOwner"):
        rel = ro.find("reportingOwnerRelationship")
        title = _find_text(rel, "officerTitle") if rel is not None else None
        if title and remarks and _SEE_REMARKS_RE.match(title.strip()):
            title = remarks
        owners.append({
            "cik": (_find_text(ro, "reportingOwnerId/rptOwnerCik") or "").lstrip("0") or None,
            "name": _find_text(ro, "reportingOwnerId/rptOwnerName"),
            "is_director": _flag(_find_text(rel, "isDirector")) if rel is not None else 0,
            "is_officer": _flag(_find_text(rel, "isOfficer")) if rel is not None else 0,
            "officer_title": title,
            "is_ten_percent_owner": _flag(_find_text(rel, "isTenPercentOwner")) if rel is not None else 0,
        })

    txns = []
    seq = 0
    for table_path, is_derivative in (
        ("nonDerivativeTable/nonDerivativeTransaction", 0),
        ("derivativeTable/derivativeTransaction", 1),
    ):
        for tx in root.findall(table_path):
            shares = _num(_value(tx, "transactionAmounts/transactionShares"))
            price = _num(_value(tx, "transactionAmounts/transactionPricePerShare"))
            fn_ids = [el.get("id") for el in tx.iter("footnoteId")]
            fn_text = "; ".join(dict.fromkeys(
                footnotes[i] for i in fn_ids if footnotes.get(i)))
            txns.append({
                "footnotes": fn_text or None,
                "txn_seq": seq,
                "is_derivative": is_derivative,
                "security_title": _value(tx, "securityTitle"),
                "transaction_date": _date(_value(tx, "transactionDate")),
                "transaction_code": _find_text(tx, "transactionCoding/transactionCode"),
                "acquired_disposed": _value(tx, "transactionAmounts/transactionAcquiredDisposedCode"),
                "shares": shares,
                "price_per_share": price,
                "value": shares * price if shares is not None and price is not None else None,
                "shares_owned_after": _num(
                    _value(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")
                ),
                "direct_indirect": _value(tx, "ownershipNature/directOrIndirectOwnership"),
            })
            seq += 1

    return {
        "document_type": _find_text(root, "documentType"),
        # Rule 10b5-1(c) plan checkbox (X0609+; absent in X0306 → 0).
        "aff_10b5_one": _flag(_find_text(root, "aff10b5One")),
        "period_of_report": _date(_find_text(root, "periodOfReport")),
        "issuer": issuer,
        "owners": owners,
        "txns": txns,
    }
