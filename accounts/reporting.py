from decimal import Decimal, InvalidOperation
from html import escape


def _coerce_decimal(value):
    if value in (None, ""):
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0.00")


def format_money(value):
    amount = _coerce_decimal(value)
    return f"ETB {amount:,.2f}"


def format_percentage(value):
    amount = _coerce_decimal(value)
    return f"{amount:.1f}%"


def _pdf_escape(text):
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _render_minimal_pdf(lines):
    content_lines = ["BT", "/F1 11 Tf", "50 790 Td", "15 TL"]
    for line in lines:
        content_lines.append(f"({_pdf_escape(line)}) Tj")
        content_lines.append("T*")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("latin-1", "replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream",
    ]

    buffer = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(buffer))
        buffer.extend(f"{index} 0 obj\n".encode("latin-1"))
        buffer.extend(obj)
        buffer.extend(b"\nendobj\n")

    xref_start = len(buffer)
    buffer.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    buffer.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        buffer.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
    buffer.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_start}\n%%EOF".encode("latin-1")
    )
    return bytes(buffer)


def build_company_report_pdf(*, company_name, date_label, summary, product_rows, agent_rows):
    lines = [
        f"{company_name} Operational Report",
        f"Date range: {date_label}",
        "",
        f"Total orders: {summary['total_orders']}",
        f"Delivered orders: {summary['delivered_orders']}",
        f"Revenue: {format_money(summary['revenue'])}",
        f"Units sold: {summary['units_sold']}",
        f"Approved refunds: {format_money(summary['approved_refunds'])}",
        "",
        "Top Products",
    ]
    for row in product_rows[:10]:
        lines.append(
            f"- {row['name']} ({row['size_label'] or 'Size not set'}): {row['units_sold']} units, {format_money(row['revenue'])}"
        )
    lines.append("")
    lines.append("Agent Performance")
    for row in agent_rows[:10]:
        lines.append(
            f"- {row['name']}: {row['delivered_orders']} delivered, {format_money(row['revenue'])}, rating {row['average_rating_display']}"
        )
    return _render_minimal_pdf(lines)


def build_company_report_excel(*, company_name, date_label, summary, product_rows, agent_rows):
    summary_rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in (
            ("Date range", date_label),
            ("Total orders", str(summary["total_orders"])),
            ("Delivered orders", str(summary["delivered_orders"])),
            ("Revenue", format_money(summary["revenue"])),
            ("Units sold", str(summary["units_sold"])),
            ("Approved refunds", format_money(summary["approved_refunds"])),
        )
    )

    product_table = "".join(
        (
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{escape(row['size_label'] or 'Size not set')}</td>"
            f"<td>{row['stock_total']}</td>"
            f"<td>{row['units_sold']}</td>"
            f"<td>{escape(format_money(row['revenue']))}</td>"
            "</tr>"
        )
        for row in product_rows
    )

    agent_table = "".join(
        (
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{escape(row['manager_name'])}</td>"
            f"<td>{row['drivers_count']}</td>"
            f"<td>{row['delivered_orders']}</td>"
            f"<td>{escape(format_money(row['revenue']))}</td>"
            f"<td>{escape(row['average_rating_display'])}</td>"
            "</tr>"
        )
        for row in agent_rows
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: Arial, sans-serif; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; }}
th {{ background: #f3f4f6; }}
h1, h2 {{ color: #003178; }}
</style>
</head>
<body>
<h1>{escape(company_name)} Operational Report</h1>
<table>{summary_rows}</table>
<h2>Product Performance</h2>
<table>
<thead>
<tr><th>Product</th><th>Size</th><th>Stock</th><th>Units Sold</th><th>Revenue</th></tr>
</thead>
<tbody>{product_table}</tbody>
</table>
<h2>Agent Performance</h2>
<table>
<thead>
<tr><th>Agent</th><th>Manager</th><th>Drivers</th><th>Delivered Orders</th><th>Revenue</th><th>Rating</th></tr>
</thead>
<tbody>{agent_table}</tbody>
</table>
</body>
</html>"""
    return html.encode("utf-8")


def build_system_report_pdf(*, date_label, summary, company_rows, growth_rows):
    lines = [
        "Platform Operational Report",
        f"Date range: {date_label}",
        "",
        f"Companies: {summary['companies']}",
        f"Verified companies: {summary['verified_companies']}",
        f"Users: {summary['users']}",
        f"Orders: {summary['orders']}",
        f"Revenue: {format_money(summary['revenue'])}",
        "",
        "User Growth",
    ]
    for row in growth_rows:
        lines.append(f"- {row['label']}: {row['count']}")
    lines.append("")
    lines.append("Company Comparison")
    for row in company_rows[:12]:
        lines.append(
            f"- {row['name']}: {row['orders']} orders, {format_money(row['revenue'])}, status {row['status']}"
        )
    return _render_minimal_pdf(lines)


def build_system_report_excel(*, date_label, summary, company_rows, growth_rows):
    summary_rows = "".join(
        f"<tr><th>{escape(label)}</th><td>{escape(value)}</td></tr>"
        for label, value in (
            ("Date range", date_label),
            ("Companies", str(summary["companies"])),
            ("Verified companies", str(summary["verified_companies"])),
            ("Users", str(summary["users"])),
            ("Orders", str(summary["orders"])),
            ("Revenue", format_money(summary["revenue"])),
        )
    )
    growth_table = "".join(
        f"<tr><td>{escape(row['label'])}</td><td>{row['count']}</td></tr>"
        for row in growth_rows
    )
    company_table = "".join(
        (
            "<tr>"
            f"<td>{escape(row['name'])}</td>"
            f"<td>{escape(row['status'])}</td>"
            f"<td>{row['agents']}</td>"
            f"<td>{row['users']}</td>"
            f"<td>{row['orders']}</td>"
            f"<td>{escape(format_money(row['revenue']))}</td>"
            "</tr>"
        )
        for row in company_rows
    )
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: Arial, sans-serif; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
th, td {{ border: 1px solid #d1d5db; padding: 8px; text-align: left; }}
th {{ background: #f3f4f6; }}
h1, h2 {{ color: #003178; }}
</style></head><body>
<h1>Platform Operational Report</h1>
<table>{summary_rows}</table>
<h2>User Growth</h2>
<table><thead><tr><th>Role</th><th>New Users</th></tr></thead><tbody>{growth_table}</tbody></table>
<h2>Company Comparison</h2>
<table>
<thead><tr><th>Company</th><th>Status</th><th>Agents</th><th>Users</th><th>Orders</th><th>Revenue</th></tr></thead>
<tbody>{company_table}</tbody>
</table>
</body></html>"""
    return html.encode("utf-8")


def build_audit_log_csv(log_rows):
    header = "Timestamp,Actor,Action,Entity Type,Entity ID,Entity Label,IP Address,Old Values,New Values\n"
    data_rows = []
    for row in log_rows:
        values = [
            row["timestamp"],
            row["actor"],
            row["action"],
            row["entity_type"],
            row["entity_id"],
            row["entity_label"],
            row["ip_address"],
            row["old_values"],
            row["new_values"],
        ]
        escaped = [f'"{str(value).replace("\"", "\"\"")}"' for value in values]
        data_rows.append(",".join(escaped))
    return (header + "\n".join(data_rows)).encode("utf-8")
