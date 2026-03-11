#!/usr/bin/env python3
"""
Medical Bill Translation Script
Parses a GitHub Issue body containing bill metadata and line items,
applies rules from rules_BIG.md against code_definitions_pack_BIG.csv,
and produces a structured Markdown comment.
"""

import csv
import io
import json
import os
import re
import sys
from datetime import datetime

NO_RESPONSE = "_No response_"


def blank(val):
    """Return True if val is empty, None, or the GitHub 'no response' sentinel."""
    if val is None:
        return True
    s = str(val).strip()
    return s == "" or s == NO_RESPONSE


def parse_money(val):
    """Parse a monetary string like '$1,200.00' or '1200' into a float."""
    s = str(val).strip().replace("$", "").replace(",", "")
    return float(s)


def fmt_money(val):
    """Format a float as $X,XXX.XX"""
    return f"${val:,.2f}"


# ---------------------------------------------------------------------------
# 1. Load code definitions
# ---------------------------------------------------------------------------
def load_code_definitions(path):
    """Load code_definitions_pack_BIG.csv into a dict keyed by code."""
    defs = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row["code"].strip()
            defs[code] = {
                "code_type": row["code_type"].strip(),
                "official_description": row["official_description"].strip(),
                "plain_english": row["plain_english"].strip(),
                "status": row["status"].strip(),
                "effective_date": row["effective_date"].strip(),
            }
    return defs


# ---------------------------------------------------------------------------
# 2. Parse issue body
# ---------------------------------------------------------------------------
def extract_section(body, heading):
    """
    Extract the content under a ### heading from the issue body.
    Returns the text between this heading and the next ### heading (or end).
    """
    pattern = rf"###\s*{re.escape(heading)}\s*\n(.*?)(?=\n###\s|\Z)"
    m = re.search(pattern, body, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def detect_delimiter(text):
    """Detect whether the pasted data is TSV or CSV."""
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return ","
    header = lines[0]
    tab_count = header.count("\t")
    comma_count = header.count(",")
    return "\t" if tab_count >= comma_count and tab_count > 0 else ","


def parse_line_items(text):
    """
    Parse pasted CSV/TSV line-item data.
    Returns (rows, errors) where rows is a list of dicts and errors is a list of error strings.
    """
    if not text or blank(text):
        return [], ["No line-item data provided."]

    # GitHub Issue Forms with render:text wrap content in code fences — strip them
    text = re.sub(r"^```[^\n]*\n?", "", text.strip())
    text = re.sub(r"\n?```\s*$", "", text.strip())

    delimiter = detect_delimiter(text)
    rows = []
    errors = []
    reader = csv.DictReader(io.StringIO(text.strip()), delimiter=delimiter)

    # Normalize header names
    if reader.fieldnames:
        reader.fieldnames = [f.strip().lower() for f in reader.fieldnames]

    required_cols = {"line_id", "date_of_service", "code", "code_type", "units", "charge"}
    if reader.fieldnames and not required_cols.issubset(set(reader.fieldnames)):
        missing = required_cols - set(reader.fieldnames)
        return [], [f"Missing required columns: {', '.join(sorted(missing))}"]

    for i, raw_row in enumerate(reader, start=2):  # row 1 is header
        row_errors = []
        line_id = (raw_row.get("line_id") or "").strip()

        # Validate line_id
        try:
            lid = int(line_id)
        except (ValueError, TypeError):
            row_errors.append("invalid line_id")
            lid = None

        # Validate date_of_service
        dos_str = (raw_row.get("date_of_service") or "").strip()
        try:
            dos = datetime.strptime(dos_str, "%Y-%m-%d").date()
        except ValueError:
            row_errors.append(f"invalid date_of_service '{dos_str}'")
            dos = None

        # Validate code
        code = (raw_row.get("code") or "").strip()
        if not code:
            row_errors.append("missing code")

        # Validate code_type
        code_type = (raw_row.get("code_type") or "").strip()
        if not code_type:
            row_errors.append("missing code_type")

        # Validate units
        units_str = (raw_row.get("units") or "").strip()
        try:
            units = int(units_str)
        except (ValueError, TypeError):
            row_errors.append(f"invalid units '{units_str}'")
            units = None

        # Validate charge
        charge_str = (raw_row.get("charge") or "").strip()
        try:
            charge = parse_money(charge_str)
        except (ValueError, TypeError):
            row_errors.append(f"invalid charge '{charge_str}'")
            charge = None

        bill_label = (raw_row.get("bill_label") or "").strip()

        if row_errors:
            lid_display = line_id if line_id else f"(row {i})"
            errors.append(f"Row {i}, line_id={lid_display}: {'; '.join(row_errors)}")
        else:
            rows.append({
                "line_id": lid,
                "date_of_service": dos,
                "code": code,
                "code_type": code_type,
                "units": units,
                "charge": charge,
                "bill_label": bill_label,
            })

    return rows, errors


# ---------------------------------------------------------------------------
# 3. Apply rules
# ---------------------------------------------------------------------------
def evaluate_line_items(rows, code_defs):
    """
    Evaluate each line item against rules.
    Returns (section2, duplicates, clarifications).
    """
    section2 = []
    clarifications = []

    for item in sorted(rows, key=lambda x: x["line_id"]):
        code = item["code"]
        code_type = item["code_type"]
        dos = item["date_of_service"]
        units = item["units"]
        charge = item["charge"]
        line_id = item["line_id"]

        notes = []
        official_desc = ""
        plain_eng = ""
        needs_clarification = False
        clarification_reasons = []

        if code not in code_defs:
            # Rule 3: missing code
            official_desc = "Definition not provided in Code Pack."
            plain_eng = "Definition not provided in Code Pack."
            needs_clarification = True
            clarification_reasons.append("Missing code definition")
        else:
            defn = code_defs[code]
            # Rule 6: code_type mismatch
            if code_type != defn["code_type"]:
                official_desc = "Definition not provided in Code Pack."
                plain_eng = "Definition not provided in Code Pack."
                needs_clarification = True
                clarification_reasons.append(
                    f"code_type mismatch: bill has '{code_type}', pack has '{defn['code_type']}'"
                )
            else:
                # Rule 4 & 5: status and effective_date
                eff_date = datetime.strptime(defn["effective_date"], "%Y-%m-%d").date()
                if defn["status"] != "Active" or eff_date > dos:
                    official_desc = "N/A"
                    plain_eng = "N/A"
                    notes.append("Code is inactive or not effective for the date of service.")
                    needs_clarification = True
                    clarification_reasons.append("Code is inactive or not effective for the date of service.")
                else:
                    official_desc = defn["official_description"]
                    plain_eng = defn["plain_english"]

        # Clarification trigger: units = 0 AND charge > 0
        if units == 0 and charge > 0:
            needs_clarification = True
            clarification_reasons.append("units = 0 AND charge > 0")

        # Clarification trigger: code_type is MOD and charge > 0
        if code_type == "MOD" and charge > 0:
            needs_clarification = True
            clarification_reasons.append("code_type is MOD with charge > 0")

        entry = {
            "line_id": line_id,
            "dos": dos.strftime("%Y-%m-%d"),
            "code": code,
            "code_type": code_type,
            "official_desc": official_desc,
            "plain_eng": plain_eng,
            "units": units,
            "charge": charge,
            "notes": "; ".join(notes) if notes else "",
        }
        section2.append(entry)

        if needs_clarification:
            clarifications.append({
                "line_id": line_id,
                "code": code,
                "reasons": clarification_reasons,
            })

    # --- Duplicate detection (Rule 7-10) ---
    # Group by (date_of_service, code, units, charge)
    from collections import defaultdict
    dup_groups_map = defaultdict(list)
    for item in sorted(rows, key=lambda x: x["line_id"]):
        key = (
            item["date_of_service"].strftime("%Y-%m-%d"),
            item["code"],
            item["units"],
            item["charge"],
        )
        dup_groups_map[key].append(item["line_id"])

    # Only keep groups with more than one member
    dup_groups = []
    dup_line_ids = set()
    group_num = 1
    for key in sorted(dup_groups_map.keys(), key=lambda k: min(dup_groups_map[k])):
        ids = dup_groups_map[key]
        if len(ids) > 1:
            dup_groups.append({
                "group": group_num,
                "line_ids": ids,
                "dos": key[0],
                "code": key[1],
                "units": key[2],
                "charge": key[3],
            })
            for lid in ids:
                dup_line_ids.add(lid)
            group_num += 1

    # Add duplicate notes to section2
    for entry in section2:
        if entry["line_id"] in dup_line_ids:
            dup_note = "This appears duplicated under the project's duplicate rule. Please confirm with billing."
            if entry["notes"]:
                entry["notes"] += "; " + dup_note
            else:
                entry["notes"] = dup_note

    return section2, dup_groups, clarifications


# ---------------------------------------------------------------------------
# 4. Format output
# ---------------------------------------------------------------------------
def format_output(header_info, section2, dup_groups, clarifications, input_errors):
    """Format the final Markdown comment."""
    lines = []

    # --- Input Problems (if any) ---
    if input_errors:
        lines.append("Input Problems")
        lines.append("")
        lines.append("The following rows could not be parsed and are excluded from the output below:")
        lines.append("")
        for err in input_errors:
            lines.append(f"- {err}")
        lines.append("")
        lines.append("⚠️ The output below is INCOMPLETE because of the skipped rows listed above.")
        lines.append("")
        lines.append("---")
        lines.append("")

    # --- SECTION 1 ---
    lines.append("SECTION 1: Bill Header Summary")
    lines.append("")
    for key, val in header_info.items():
        lines.append(f"- **{key}:** {val}")
    lines.append("")

       # --- SECTION 2 ---
    lines.append("SECTION 2: Plain-English Line Item Table")
    lines.append("")
    lines.append("| Line # | DOS | Code | Code Type | Official Description | Plain-English | Units | Charge |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for e in section2:
        charge_str = fmt_money(e["charge"])
        lines.append(
            f"| {e['line_id']} | {e['dos']} | {e['code']} | {e['code_type']} "
            f"| {e['official_desc']} | {e['plain_eng']} | {e['units']} | {charge_str} |"
        )
    lines.append("")

    # --- SECTION 3 ---
    lines.append("SECTION 3: Duplicates Table")
    lines.append("")
    if not dup_groups:
        lines.append("No duplicates found under the project's duplicate rule.")
    else:
        lines.append("| Duplicate Group | Line #s | Matching Fields | Suggested question for billing |")
        lines.append("|---|---|---|---|")
        for g in dup_groups:
            ids_str = ", ".join(str(lid) for lid in g["line_ids"])
            matching = f"DOS={g['dos']}, Code={g['code']}, Units={g['units']}, Charge={fmt_money(g['charge'])}"
            question = "This appears duplicated under the project's duplicate rule. Please confirm with billing."
            lines.append(f"| {g['group']} | {ids_str} | {matching} | {question} |")
    lines.append("")

    # --- SECTION 4 ---
    lines.append("SECTION 4: Needs Clarification List")
    lines.append("")
    if not clarifications:
        lines.append("No items require clarification.")
    else:
        for c in clarifications:
            for reason in c["reasons"]:
                lines.append(f"- Clarify: Line {c['line_id']} (Code {c['code']}): {reason}")
    lines.append("")

    # --- SECTION 5 ---
    total_charge = sum(e["charge"] for e in section2)
    num_items = len(section2)
    num_dups = sum(len(g["line_ids"]) for g in dup_groups)
    num_clar = len(clarifications)

    dos_set = sorted(set(e["dos"] for e in section2))
    dos_range = f"{dos_set[0]} to {dos_set[-1]}" if len(dos_set) > 1 else dos_set[0]

    lines.append("SECTION 5: Patient-Friendly Summary Paragraph")
    lines.append("")

    summary_parts = []
    summary_parts.append(
        f"This bill contains {num_items} line items spanning dates of service from {dos_range}."
    )
    summary_parts.append(
        f"The total billed amount across all line items is {fmt_money(total_charge)}."
    )
    summary_parts.append(
        "Services include procedures, facility fees, medications, and modifiers."
    )
    if dup_groups:
        summary_parts.append(
            f"The review identified {len(dup_groups)} duplicate group(s) involving {num_dups} line items "
            f"where the date of service, code, units, and charge all matched."
        )
        summary_parts.append(
            "You may wish to confirm these with your billing department to ensure they are not billed twice."
        )
    else:
        summary_parts.append("No duplicate charges were identified under the project's duplicate rule.")
    if num_clar > 0:
        summary_parts.append(
            f"{num_clar} line item(s) have been flagged for clarification due to missing definitions, "
            f"inactive codes, or other triggers described in the rules."
        )
    summary_parts.append(
        "This summary is provided for informational purposes only and does not constitute medical or billing advice."
    )
    summary_parts.append(
        "Please review each section and contact your billing department with any questions."
    )

    lines.append(" ".join(summary_parts))
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. Main entry point
# ---------------------------------------------------------------------------
def main():
    issue_body = os.environ.get("ISSUE_BODY", "")
    code_defs_path = os.environ.get("CODE_DEFS_PATH", "code_definitions_pack_BIG.csv")
    output_file = os.environ.get("OUTPUT_FILE", "comment.md")

    # --- Gate check: expected headings ---
    if "### Line Items" not in issue_body:
        print("Issue body does not contain expected form headings. Skipping.")
        sys.exit(0)

    # --- Load code definitions ---
    try:
        code_defs = load_code_definitions(code_defs_path)
    except Exception as e:
        with open(output_file, "w") as f:
            f.write(f"❌ **Error:** Could not load code definitions file: {e}\n")
        sys.exit(1)

    # --- Parse header fields ---
    provider_name = extract_section(issue_body, "Provider Name") or ""
    if blank(provider_name):
        provider_name = ""
    facility_name = extract_section(issue_body, "Facility Name") or ""
    if blank(facility_name):
        facility_name = ""
    bill_date = extract_section(issue_body, "Bill Date") or ""
    if blank(bill_date):
        bill_date = ""
    patient_account = extract_section(issue_body, "Patient Account Number") or ""
    if blank(patient_account):
        patient_account = ""
    total_billed_raw = extract_section(issue_body, "Total Billed") or ""
    if blank(total_billed_raw):
        total_billed_raw = ""

    # --- Parse line items ---
    line_items_text = extract_section(issue_body, "Line Items")
    rows, parse_errors = parse_line_items(line_items_text)

    if not rows and parse_errors:
        with open(output_file, "w") as f:
            f.write("❌ **Error:** Could not parse any line items.\n\n")
            for err in parse_errors:
                f.write(f"- {err}\n")
        sys.exit(1)

    # --- Evaluate ---
    section2, dup_groups, clarifications = evaluate_line_items(rows, code_defs)

    # --- Build header info ---
    computed_total = sum(e["charge"] for e in section2)

    header_info = {}
    if provider_name:
        header_info["Provider Name"] = provider_name
    if facility_name:
        header_info["Facility Name"] = facility_name
    if bill_date:
        header_info["Bill Date"] = bill_date
    if patient_account:
        header_info["Patient Account Number"] = patient_account

    if total_billed_raw:
        header_info["Total Billed (Provided)"] = total_billed_raw
    header_info["Total Billed (Computed)"] = fmt_money(computed_total)

    # --- Format output ---
    comment = format_output(header_info, section2, dup_groups, clarifications, parse_errors)

    with open(output_file, "w") as f:
        f.write(comment)

    print(f"Output written to {output_file}")


if __name__ == "__main__":
    main()
