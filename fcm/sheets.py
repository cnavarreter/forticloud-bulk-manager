"""Read and write CSV / XLSX tables using only the Python standard library."""

import csv
import io
import re
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


class SheetError(Exception):
    pass


# --------------------------------------------------------------------------- reading

def read_table(filename, data):
    """Return (headers, rows) where rows is a list of lists of strings.

    `data` is bytes for files, or str for pasted text.
    """
    name = (filename or "").lower()
    if isinstance(data, bytes) and (name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK"):
        table = _read_xlsx(data)
    else:
        text = data if isinstance(data, str) else _decode(data)
        table = _read_delimited(text)
    # drop fully empty rows
    table = [[(c or "").strip() for c in r] for r in table]
    table = [r for r in table if any(c for c in r)]
    if not table:
        raise SheetError("The file is empty.")
    headers = table[0]
    width = len(headers)
    rows = []
    for r in table[1:]:
        r = list(r) + [""] * (width - len(r))
        rows.append(r[:max(width, len(r))])
    return headers, rows


def _decode(data):
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise SheetError("Could not decode the file. Save it as UTF-8 CSV.")


def _read_delimited(text):
    sample = text[:4096]
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    if "\t" in first_line:
        delim = "\t"
    elif first_line.count(";") > first_line.count(","):
        delim = ";"
    else:
        delim = ","
    return list(csv.reader(io.StringIO(text), delimiter=delim))


def _col_index(ref):
    letters = re.match(r"[A-Z]+", ref or "")
    if not letters:
        return None
    n = 0
    for ch in letters.group(0):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _num_to_str(val):
    try:
        f = float(val)
    except (TypeError, ValueError):
        return val
    if f.is_integer() and abs(f) < 1e15:
        return str(int(f))
    return val


def _read_xlsx(data):
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SheetError("This does not look like a valid .xlsx file.")
    m = "{%s}" % _NS_MAIN
    names = set(z.namelist())

    shared = []
    if "xl/sharedStrings.xml" in names:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in root.findall(m + "si"):
            shared.append("".join(t.text or "" for t in si.iter(m + "t")))

    wb = ET.fromstring(z.read("xl/workbook.xml"))
    sheets = wb.find(m + "sheets")
    if sheets is None or len(sheets) == 0:
        raise SheetError("The workbook has no sheets.")
    rid = sheets[0].get("{%s}id" % _NS_REL)
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = None
    for rel in rels:
        if rel.get("Id") == rid:
            target = rel.get("Target")
    if not target:
        raise SheetError("Could not locate the first worksheet.")
    path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    sheet = ET.fromstring(z.read(path))

    table = []
    for r in sheet.iter(m + "row"):
        cells = {}
        auto_col = 0
        for c in r.findall(m + "c"):
            idx = _col_index(c.get("r"))
            if idx is None:
                idx = auto_col
            auto_col = idx + 1
            t = c.get("t")
            v = c.find(m + "v")
            if t == "s":
                val = shared[int(v.text)] if v is not None and v.text else ""
            elif t == "inlineStr":
                val = "".join(x.text or "" for x in c.iter(m + "t"))
            elif t == "b":
                val = "TRUE" if (v is not None and v.text == "1") else "FALSE"
            else:
                val = v.text if v is not None and v.text is not None else ""
                if t in (None, "n") and val:
                    val = _num_to_str(val)
            cells[idx] = val
        row_no = r.get("r")
        if row_no and row_no.isdigit():
            while len(table) < int(row_no) - 1:
                table.append([])
        width = (max(cells) + 1) if cells else 0
        table.append([cells.get(i, "") for i in range(width)])
    return table


# --------------------------------------------------------------------------- writing

def write_csv(headers, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return buf.getvalue().encode("utf-8-sig")


_BAD_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _cell_ref(col, row):
    s = ""
    col += 1
    while col:
        col, rem = divmod(col - 1, 26)
        s = chr(65 + rem) + s
    return "%s%d" % (s, row)


def _sheet_xml(rows, widths, freeze_header=True):
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="%s">' % _NS_MAIN]
    if freeze_header:
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
                   '</sheetView></sheetViews>')
    if widths:
        out.append("<cols>")
        for i, wdt in enumerate(widths):
            out.append('<col min="%d" max="%d" width="%d" customWidth="1"/>' % (i + 1, i + 1, wdt))
        out.append("</cols>")
    out.append("<sheetData>")
    for ri, row in enumerate(rows):
        out.append('<row r="%d">' % (ri + 1))
        for ci, val in enumerate(row):
            if val is None or val == "":
                continue
            text = escape(_BAD_XML.sub("", str(val)))
            style = ' s="1"' if (ri == 0 and freeze_header) else ' s="2"'
            out.append('<c r="%s" t="inlineStr"%s><is><t xml:space="preserve">%s</t></is></c>'
                       % (_cell_ref(ci, ri + 1), style, text))
        out.append("</row>")
    out.append("</sheetData></worksheet>")
    return "".join(out)


def write_xlsx(sheets):
    """sheets: list of (name, rows) where rows[0] is the header row."""
    buf = io.BytesIO()
    z = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
          '<Default Extension="xml" ContentType="application/xml"/>',
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
          '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(len(sheets)):
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    ct.append("</Types>")
    z.writestr("[Content_Types].xml", "".join(ct))
    z.writestr("_rels/.rels",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<Relationships xmlns="%s"><Relationship Id="rId1" '
               'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
               'Target="xl/workbook.xml"/></Relationships>' % _NS_PKG_REL)
    wb = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<workbook xmlns="%s" xmlns:r="%s"><sheets>' % (_NS_MAIN, _NS_REL)]
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<Relationships xmlns="%s">' % _NS_PKG_REL]
    for i, (name, rows) in enumerate(sheets):
        safe = escape(re.sub(r"[\[\]\*\?/\\:]", "", name)[:31] or "Sheet%d" % (i + 1))
        wb.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (safe, i + 1, i + 1))
        rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1))
        ncols = max((len(r) for r in rows), default=0)
        widths = []
        for c in range(ncols):
            longest = max((len(str(r[c])) for r in rows if c < len(r) and r[c] is not None), default=8)
            widths.append(min(max(longest + 2, 10), 70))
        z.writestr("xl/worksheets/sheet%d.xml" % (i + 1), _sheet_xml(rows, widths))
    rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (len(sheets) + 1))
    wb.append("</sheets></workbook>")
    rels.append("</Relationships>")
    z.writestr("xl/workbook.xml", "".join(wb))
    z.writestr("xl/_rels/workbook.xml.rels", "".join(rels))
    # style 0 default, 1 bold header with fill, 2 text format (@) so serials stay text
    z.writestr("xl/styles.xml",
               '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
               '<styleSheet xmlns="%s">'
               '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
               '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
               '<fills count="3"><fill><patternFill patternType="none"/></fill>'
               '<fill><patternFill patternType="gray125"/></fill>'
               '<fill><patternFill patternType="solid"><fgColor rgb="FFE8EAED"/><bgColor indexed="64"/></patternFill></fill></fills>'
               '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
               '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
               '<cellXfs count="3"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
               '<xf numFmtId="49" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyNumberFormat="1"/>'
               '<xf numFmtId="49" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
               '</styleSheet>' % _NS_MAIN)
    z.close()
    return buf.getvalue()
