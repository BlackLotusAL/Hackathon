"""The value-only XLSX format used for preliminary roster and result exchange."""
from io import BytesIO
from itertools import chain
import posixpath
import re
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


SHEET_XML = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
DOC_REL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG_REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
TYPES = 'http://schemas.openxmlformats.org/package/2006/content-types'
MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
MAX_UPLOAD = 16 * 1024 * 1024
MAX_TABLE_XML = 512 * 1024 * 1024
MAX_ROWS = 1048576
CELL_REF = re.compile(r'([A-Z]+)[1-9][0-9]*')
ROSTER_HEADERS = ('队伍编号', '队名')
RANK_HEADERS = ('名次', '队伍编号', '胜局数', '负局数')
DUEL_HEADERS = ('duel_id', '队伍1编号', '队伍2编号', '队伍1胜局数', '队伍1负局数',
                '队伍2胜局数', '队伍2负局数', '整场结果', '胜方', '判定原因', 'build_run_id')
GAME_HEADERS = ('duel_id', '预定局序', 'run_id')


class WorkbookError(ValueError):
    pass


def _ns(node):
    return node.tag.rpartition('}')[0] + '}' if node.tag.startswith('{') else ''


def _part(package, name):
    entry = package.getinfo(name)
    if entry.file_size > MAX_UPLOAD:
        raise WorkbookError(f'工作簿部件过大 ({name})')
    data = package.read(entry)
    if b'<!DOCTYPE' in data.replace(b'\0', b''):
        raise WorkbookError('工作簿包含不支持的 XML 声明')
    return ET.fromstring(data)


def _relationships(package, source):
    directory, filename = posixpath.split(source)
    root = _part(package, posixpath.join(directory, '_rels', filename + '.rels'))
    return {item.attrib['Id']: item.attrib for item in root if item.get('TargetMode') != 'External'}


def _target(source, relationship):
    target = relationship['Target']
    base = posixpath.dirname(source) if not target.startswith('/') else ''
    return posixpath.normpath(posixpath.join(base, target.lstrip('/')))


def _text(node, ns):
    if node is None:
        return ''
    return ''.join(t.text or '' for t in node.findall(ns + 't') + node.findall(f'{ns}r/{ns}t'))


def _value(cell, ns, shared, sheet, row):
    if cell is None:
        return ''
    if cell.find(ns + 'f') is not None or cell.get('t') == 'e':
        raise WorkbookError(f'「{sheet}」第 {row} 行须使用固定值')
    value = cell.findtext(ns + 'v', '')
    if cell.get('t') == 's':
        index = int(value)
        if not 0 <= index < len(shared):
            raise WorkbookError(f'「{sheet}」第 {row} 行引用无效的共享字符串')
        value = shared[index]
    elif cell.get('t') == 'inlineStr':
        value = _text(cell.find(ns + 'is'), ns)
    return value.strip()


def _column(number):
    letters = ''
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord('A') + remainder) + letters
    return letters


class _XMLStream:
    def __init__(self, stream):
        self.stream, self.tail = stream, b''

    def read(self, size):
        data = self.stream.read(size)
        scanned = self.tail + data.replace(b'\0', b'')
        if b'<!DOCTYPE' in scanned:
            raise WorkbookError('工作簿包含不支持的 XML 声明')
        self.tail = scanned[-8:]
        return data


def _items(package, path, parent, item):
    entry = package.getinfo(path)
    if entry.file_size > MAX_TABLE_XML:
        raise WorkbookError(f'工作簿部件过大 ({path})')
    with package.open(entry) as stream:
        stack = []
        for event, element in ET.iterparse(_XMLStream(stream), events=('start', 'end')):
            if event == 'start':
                if not stack:
                    ns = _ns(element)
                stack.append(element)
                continue
            if element.tag == ns + item and len(stack) > 1 and stack[-2].tag == ns + parent:
                yield ns, element
                stack[-2].remove(element)
            stack.pop()


class _Workbook:
    def __init__(self, package):
        self.package = package
        root_rels = _relationships(package, '')
        work_rel = next((rel for rel in root_rels.values() if rel['Type'].endswith('/officeDocument')), None)
        if work_rel is None:
            raise WorkbookError('文件缺少 XLSX 工作簿')
        path = _target('', work_rel)
        book = _part(package, path)
        ns = _ns(book)
        self.sheets = {}
        relationships = _relationships(package, path)
        for sheet in book.findall(f'{ns}sheets/{ns}sheet'):
            name = sheet.attrib['name']
            if name in self.sheets:
                raise WorkbookError(f'工作簿的「{name}」表重复')
            rel = next(value for key, value in sheet.attrib.items() if key.endswith('}id'))
            self.sheets[name] = _target(path, relationships[rel])
        self.shared = []
        shared_rel = next((rel for rel in relationships.values() if rel['Type'].endswith('/sharedStrings')), None)
        if shared_rel is not None:
            strings = _items(package, _target(path, shared_rel), 'sst', 'si')
            self.shared = [_text(item, item_ns) for item_ns, item in strings]

    def rows(self, name):
        if name not in self.sheets:
            raise WorkbookError(f'工作簿缺少「{name}」表')
        for index, (ns, row) in enumerate(_items(self.package, self.sheets[name], 'sheetData', 'row'), 1):
            number = int(row.get('r', index))
            if not 1 <= number <= MAX_ROWS:
                raise WorkbookError(f'「{name}」表的行号无效')
            cells = {CELL_REF.fullmatch(cell.get('r', f'{_column(i)}{number}'))[1]: cell
                     for i, cell in enumerate(row.findall(ns + 'c'), 1)}
            yield number, cells, ns


def _header(rows, sheet, shared):
    for number, cells, ns in rows:
        columns = {col: _value(cell, ns, shared, sheet, number) for col, cell in cells.items()}
        if any(columns.values()):
            return columns
    raise WorkbookError(f'「{sheet}」表缺少表头')


def read_roster(data):
    """Return row numbers and literal team ID/name pairs from the roster sheet."""
    if len(data) > MAX_UPLOAD:
        raise WorkbookError('XLSX 队伍名单过大')
    try:
        with ZipFile(BytesIO(data)) as package:
            book = _Workbook(package)
            rows = book.rows('队伍')
            columns = _header(rows, '队伍', book.shared)
            ids = [col for col, title in columns.items() if title in ('队伍编号', 'team_id')]
            names = [col for col, title in columns.items() if title in ('队名', '队伍名称', 'team_name')]
            if len(ids) != 1 or len(names) > 1:
                raise WorkbookError('「队伍」表须有一列队伍编号，可选一列队名')
            roster = []
            for number, cells, ns in rows:
                team = _value(cells.get(ids[0]), ns, book.shared, '队伍', number)
                name = _value(cells.get(names[0]), ns, book.shared, '队伍', number) if names else ''
                if team or name:
                    roster.append((number, team, name))
            return roster
    except WorkbookError:
        raise
    except (BadZipFile, ET.ParseError, AttributeError, IndexError, KeyError, OSError,
            RuntimeError, StopIteration, TypeError, ValueError) as error:
        raise WorkbookError('无法读取 XLSX 队伍名单') from error


def _xml(root):
    return ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _worksheet(package, path, headers, rows, count):
    if count + 1 > MAX_ROWS:
        raise WorkbookError('结果超出 XLSX 单张工作表的行数上限')
    root = ET.Element('worksheet', {'xmlns': SHEET_XML})
    ET.SubElement(root, 'dimension', ref=f'A1:{_column(len(headers))}{count + 1}')
    views = ET.SubElement(root, 'sheetViews')
    view = ET.SubElement(views, 'sheetView', workbookViewId='0')
    ET.SubElement(view, 'pane', ySplit='1', topLeftCell='A2', activePane='bottomLeft', state='frozen')
    cols = ET.SubElement(root, 'cols')
    widths = {'duel_id': 47, 'build_run_id': 22, 'run_id': 20, '判定原因': 21, '队名': 35}
    for index, header in enumerate(headers, 1):
        width = widths.get(header, max(13, 2 * len(header) + 3))
        ET.SubElement(cols, 'col', min=str(index), max=str(index), width=str(width), customWidth='1')
    header_xml = _xml(root).removesuffix(b'</worksheet>')
    with package.open(path, 'w') as stream:
        stream.write(header_xml + b'<sheetData>')
        for index, values in enumerate(chain((headers,), rows), 1):
            row = ET.Element('row', r=str(index))
            for number, value in enumerate(values, 1):
                if value is None:
                    continue
                cell = ET.SubElement(row, 'c', r=f'{_column(number)}{index}')
                if isinstance(value, str):
                    cell.set('t', 'inlineStr')
                    inline = ET.SubElement(cell, 'is')
                    ET.SubElement(inline, 't', {'xml:space': 'preserve'}).text = value
                else:
                    ET.SubElement(cell, 'v').text = str(value)
            stream.write(ET.tostring(row, encoding='utf-8'))
        stream.write(b'</sheetData></worksheet>')


def write_workbook(sheets):
    """Write (name, headers, rows, row count) worksheets of literal values."""
    output = BytesIO()
    workbook = ET.Element('workbook', {'xmlns': SHEET_XML, 'xmlns:r': DOC_REL})
    views = ET.SubElement(workbook, 'bookViews')
    ET.SubElement(views, 'workbookView')
    entries = ET.SubElement(workbook, 'sheets')
    root_rels = ET.Element('Relationships', {'xmlns': PKG_REL})
    ET.SubElement(root_rels, 'Relationship', Id='rId1', Type=DOC_REL + '/officeDocument', Target='xl/workbook.xml')
    rels = ET.Element('Relationships', {'xmlns': PKG_REL})
    types = ET.Element('Types', {'xmlns': TYPES})
    ET.SubElement(types, 'Default', Extension='rels', ContentType='application/vnd.openxmlformats-package.relationships+xml')
    ET.SubElement(types, 'Default', Extension='xml', ContentType='application/xml')
    ET.SubElement(types, 'Override', PartName='/xl/workbook.xml', ContentType=MIME + '.main+xml')

    with ZipFile(output, 'w', ZIP_DEFLATED) as package:
        for index, (name, headers, rows, count) in enumerate(sheets, 1):
            rel_id, path = f'rId{index}', f'worksheets/sheet{index}.xml'
            ET.SubElement(entries, 'sheet', {'name': name, 'sheetId': str(index), 'r:id': rel_id})
            ET.SubElement(rels, 'Relationship', Id=rel_id, Type=DOC_REL + '/worksheet', Target=path)
            ET.SubElement(types, 'Override', PartName='/xl/' + path,
                          ContentType='application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml')
            _worksheet(package, 'xl/' + path, headers, rows, count)
        package.writestr('[Content_Types].xml', _xml(types))
        package.writestr('_rels/.rels', _xml(root_rels))
        package.writestr('xl/workbook.xml', _xml(workbook))
        package.writestr('xl/_rels/workbook.xml.rels', _xml(rels))
    return output.getvalue()

