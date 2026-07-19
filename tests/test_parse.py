"""Deterministic parser tests for the edge cases observed in real EDGAR filings.

Run:  python tests/test_parse.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.parse import parse_form4  # noqa: E402


def wrap(xml_body: str) -> str:
    return f"<SEC-DOCUMENT>\n<DOCUMENT>\n<XML>\n{xml_body}\n</XML>\n</DOCUMENT>\n</SEC-DOCUMENT>"


EDGE_CASE_DOC = wrap("""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0306</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-07-01</periodOfReport>
  <issuer>
    <issuerCik>0000012345</issuerCik>
    <issuerName>Test &amp; Co</issuerName>
    <issuerTradingSymbol>TST</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000067890</rptOwnerCik>
      <rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>true</isOfficer>
      <officerTitle>See Remarks*</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000067891</rptOwnerCik>
      <rptOwnerName>Fund LP</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isTenPercentOwner>1</isTenPercentOwner>
      <officerTitle></officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-30-05:00</value></transactionDate>
      <deemedExecutionDate></deemedExecutionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>P</transactionCode>
        <equitySwapInvolved>false</equitySwapInvolved>
      </transactionCoding>
      <transactionTimeliness><value></value></transactionTimeliness>
      <transactionAmounts>
        <transactionShares><value>1034.50</value><footnoteId id="F1"/></transactionShares>
        <transactionPricePerShare><footnoteId id="F2"/></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <valueOwnedFollowingTransaction><value>999</value></valueOwnedFollowingTransaction>
      </postTransactionAmounts>
      <ownershipNature>
        <directOrIndirectOwnership><value>I</value><footnoteId id="F2"/><footnoteId id="F3"/></directOrIndirectOwnership>
        <natureOfOwnership><value></value></natureOfOwnership>
      </ownershipNature>
    </nonDerivativeTransaction>
    <nonDerivativeHolding>
      <securityTitle><value>Common Stock</value></securityTitle>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeHolding>
  </nonDerivativeTable>
  <derivativeTable></derivativeTable>
  <remarks>Chief Legal Officer</remarks>
  <footnotes>
    <footnote id="F1">includes DRIP shares</footnote>
    <footnote id="F2">weighted average price</footnote>
    <footnote id="F3">held by trust</footnote>
  </footnotes>
</ownershipDocument>""")


HOLDINGS_ONLY_DOC = wrap("""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4/A</documentType>
  <periodOfReport>2026-06-17</periodOfReport>
  <dateOfOriginalSubmission>2026-06-17</dateOfOriginalSubmission>
  <issuer>
    <issuerCik>0000099999</issuerCik>
    <issuerName>HoldCo</issuerName>
    <issuerTradingSymbol></issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0000011111</rptOwnerCik>
      <rptOwnerName>Smith Bob</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isOfficer>1</isOfficer>
      <officerTitle>EVP, CAO, Gen Counsel &amp; Sec</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable></nonDerivativeTable>
  <derivativeTable>
    <derivativeHolding>
      <securityTitle><value>Stock Option</value></securityTitle>
      <exerciseDate><footnoteId id="F1"/></exerciseDate>
    </derivativeHolding>
  </derivativeTable>
  <footnotes><footnote id="F1">vests later</footnote></footnotes>
</ownershipDocument>""")


def test_edge_case_doc():
    p = parse_form4(EDGE_CASE_DOC)
    assert p["issuer"]["cik"] == "12345", p["issuer"]
    assert p["issuer"]["name"] == "Test & Co"          # entity unescaped
    assert len(p["owners"]) == 2
    o1, o2 = p["owners"]
    assert o1["is_officer"] == 1                       # 'true' accepted
    assert o1["officer_title"] == "Chief Legal Officer"  # 'See Remarks*' resolved
    assert o1["is_director"] == 0                      # missing flag -> 0
    assert o2["is_ten_percent_owner"] == 1             # '1' accepted
    assert o2["officer_title"] is None                 # empty element -> None
    assert len(p["txns"]) == 1, "holding row must not count as a transaction"
    t = p["txns"][0]
    assert t["transaction_date"] == "2026-06-30"       # tz suffix stripped
    assert t["transaction_code"] == "P" and t["acquired_disposed"] == "A"
    assert t["shares"] == 1034.50                      # decimal shares
    assert t["price_per_share"] is None                # footnote-only price
    assert t["value"] is None                          # never coerced to 0
    assert t["shares_owned_after"] is None             # valueOwned..., not shares
    assert t["direct_indirect"] == "I"


def test_holdings_only_doc():
    p = parse_form4(HOLDINGS_ONLY_DOC)
    assert p["document_type"] == "4/A"
    assert p["txns"] == []                             # zero transactions is valid
    assert p["issuer"]["ticker"] is None               # empty symbol -> None
    assert p["owners"][0]["officer_title"] == "EVP, CAO, Gen Counsel & Sec"


if __name__ == "__main__":
    test_edge_case_doc()
    test_holdings_only_doc()
    print("all parser tests passed")
