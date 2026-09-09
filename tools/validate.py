#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Валідатор артефактів курсу «Управління ІТ проєктами».

Аналог `flutter analyze` для PM-документів: перевіряє механіку портфеля, щоб
формальні зауваження студент бачив до дедлайну, а не після перевірки.

Спека колонок і правил лежить у `tools/schemas.json` поруч зі скриптом. Скрипт
перевіряє те, що знайшов: якщо папки роботи ще немає, це не помилка, курс
триває семестр.

Використання:
    python3 tools/validate.py            # весь репозиторій
    python3 tools/validate.py lr06_backlog   # тільки одна папка

Код виходу 0, якщо помилок рівня error немає. Залежностей поза стандартною
бібліотекою немає, Python 3.9 і новіші.
"""

import csv
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SPEC_PATH = os.path.join(HERE, 'schemas.json')

ERROR = 'error'
WARNING = 'warning'
SKIPPED = 'skipped'

LEVEL_NAME = {ERROR: 'помилка', WARNING: 'попередження', SKIPPED: 'пропущено'}

DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
INT_RE = re.compile(r'^-?\d+$')
NUMBER_RE = re.compile(r'^-?\d+(\.\d+)?$')
SOURCE_REF_RE = re.compile(r'\[(SRC-\d{2})\]')
VERSION_RE = re.compile(r'(\d+\.\d+\.\d+)')
DIGIT_RE = re.compile(r'\d')
PERCENT_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:%|відсот)')

TEAM_OWNER_WORDS = {'команда', 'вся команда', 'всі', 'все', 'усі', 'team', 'all'}


class Table:
    """Прочитаний CSV: заголовок, рядки і номери рядків у файлі."""

    def __init__(self, path, header, rows, line_numbers):
        self.path = path
        self.header = header
        self.rows = rows
        self.line_numbers = line_numbers

    def col(self, name):
        """Значення однієї колонки списком, порожній рядок якщо колонки немає."""
        if name not in self.header:
            return ['' for _ in self.rows]
        i = self.header.index(name)
        return [r[i] if i < len(r) else '' for r in self.rows]

    def cell(self, row, name):
        if name not in self.header:
            return ''
        i = self.header.index(name)
        return row[i] if i < len(row) else ''

    def line(self, index):
        return self.line_numbers[index]


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, path, line, rule, message):
        self.items.append((level, path, line, rule, message))

    def errors(self):
        return [i for i in self.items if i[0] == ERROR]

    def warnings(self):
        return [i for i in self.items if i[0] == WARNING]

    def skipped(self):
        return [i for i in self.items if i[0] == SKIPPED]


def load_spec():
    with open(SPEC_PATH, encoding='utf-8') as fh:
        return json.load(fh)


def read_table(root, rel_path):
    """Читає CSV терпимо до BOM, CRLF і порожніх хвостових рядків."""
    full = os.path.join(root, rel_path)
    with open(full, encoding='utf-8-sig', newline='') as fh:
        raw = list(csv.reader(fh))
    if not raw:
        return Table(rel_path, [], [], []), []
    header = [c.strip() for c in raw[0]]
    rows = []
    lines = []
    notes = []
    for i, row in enumerate(raw[1:], start=2):
        cells = [c.strip() for c in row]
        if not any(cells):
            if any(any(r) for r in raw[i:]):
                notes.append((WARNING, i, 'порожній рядок усередині файла, пропущено'))
            continue
        rows.append(cells)
        lines.append(i)
    return Table(rel_path, header, rows, lines), notes


def parse_date(value):
    if not DATE_RE.match(value):
        return None
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def check_header(spec_file, table, report):
    expected = [c['name'] for c in spec_file['columns']]
    if table.header == expected:
        return True
    missing = [c for c in expected if c not in table.header]
    extra = [c for c in table.header if c not in expected]
    if missing or extra:
        report.add(ERROR, table.path, 1, 'header',
                   'рядок заголовків не збігається зі спекою. Немає колонок: %s. Зайві колонки: %s. '
                   'Очікується дослівно: %s'
                   % (', '.join(missing) or 'немає', ', '.join(extra) or 'немає', ','.join(expected)))
        return False
    report.add(ERROR, table.path, 1, 'header',
               'колонки ті самі, але в іншому порядку. Очікується дослівно: %s' % ','.join(expected))
    return True


def check_mechanics(spec_file, table, enums, report):
    columns = {c['name']: c for c in spec_file['columns']}
    min_rows = spec_file.get('min_rows', 1)
    if len(table.rows) < min_rows:
        report.add(ERROR, table.path, 1, 'min_rows',
                   'рядків %d, а за спекою потрібно щонайменше %d' % (len(table.rows), min_rows))

    for idx, row in enumerate(table.rows):
        line = table.line(idx)
        if len(row) != len(table.header):
            report.add(ERROR, table.path, line, 'row_width',
                       'у рядку %d значень, а колонок %d' % (len(row), len(table.header)))
        for name, col in columns.items():
            value = table.cell(row, name)
            if not value:
                if col.get('required'):
                    report.add(ERROR, table.path, line, 'required',
                               'колонка `%s` порожня, а вона обов\'язкова' % name)
                continue
            check_value(table, line, name, col, value, enums, report)

    for name, col in columns.items():
        if not col.get('unique'):
            continue
        seen = {}
        for idx, row in enumerate(table.rows):
            value = table.cell(row, name)
            if not value:
                continue
            if value in seen:
                report.add(ERROR, table.path, table.line(idx), 'unique',
                           'значення `%s` у колонці `%s` уже було в рядку %d, а воно має бути унікальним'
                           % (value, name, seen[value]))
            else:
                seen[value] = table.line(idx)


def check_value(table, line, name, col, value, enums, report):
    kind = col['type']
    if col.get('list') or kind == 'id_list':
        parts = [p.strip() for p in value.split(';')]
        if any(not p for p in parts):
            report.add(ERROR, table.path, line, 'list',
                       'колонка `%s`: список через `;` містить порожній елемент' % name)
        min_items = col.get('min_items', 1)
        if len(parts) < min_items:
            report.add(ERROR, table.path, line, 'min_items',
                       'колонка `%s`: елементів %d, а за спекою потрібно щонайменше %d, '
                       'розділювач це крапка з комою' % (name, len(parts), min_items))
        if kind == 'id_list' and col.get('pattern'):
            for p in parts:
                if p and not re.match(col['pattern'], p):
                    report.add(ERROR, table.path, line, 'pattern',
                               'колонка `%s`: значення `%s` не відповідає формату `%s`'
                               % (name, p, col['pattern']))
        return

    if col.get('pattern') and not re.match(col['pattern'], value):
        report.add(ERROR, table.path, line, 'pattern',
                   'колонка `%s`: значення `%s` не відповідає формату `%s`'
                   % (name, value, col['pattern']))
        return

    if kind == 'enum':
        allowed = enums[col['enum']]
        if value not in allowed:
            report.add(ERROR, table.path, line, 'enum',
                       'колонка `%s`: значення `%s` поза словником, дозволено: %s'
                       % (name, value, ', '.join(allowed)))
        return

    if kind in ('int', 'number'):
        pattern = INT_RE if kind == 'int' else NUMBER_RE
        if not pattern.match(value):
            report.add(ERROR, table.path, line, 'type',
                       'колонка `%s`: значення `%s` не є числом%s'
                       % (name, value, ' (ціле)' if kind == 'int' else ', десятковий роздільник це крапка'))
            return
        number = float(value)
        if 'min' in col and number < col['min']:
            report.add(ERROR, table.path, line, 'range',
                       'колонка `%s`: значення %s менше за мінімум %s' % (name, value, col['min']))
        if 'max' in col and number > col['max']:
            report.add(ERROR, table.path, line, 'range',
                       'колонка `%s`: значення %s більше за максимум %s' % (name, value, col['max']))
        return

    if kind == 'date' and parse_date(value) is None:
        report.add(ERROR, table.path, line, 'date',
                   'колонка `%s`: дата `%s` не у форматі YYYY-MM-DD або не існує в календарі'
                   % (name, value))


def check_refs(spec_file, table, tables, report):
    for col in spec_file['columns']:
        ref = col.get('ref')
        if not ref:
            continue
        target_path, target_col = ref.split(':')
        target = tables.get(target_path)
        if target is None:
            report.add(SKIPPED, table.path, 1, 'ref',
                       'посилання колонки `%s` не перевірені: файла `%s` ще немає'
                       % (col['name'], target_path))
            continue
        known = set(v for v in target.col(target_col) if v)
        for idx, row in enumerate(table.rows):
            value = table.cell(row, col['name'])
            if not value:
                continue
            for token in [p.strip() for p in value.split(';')]:
                if token and token not in known:
                    report.add(ERROR, table.path, table.line(idx), 'ref',
                               'колонка `%s`: ключа `%s` немає в `%s`'
                               % (col['name'], token, target_path))


# ---------------------------------------------------------------- правила файлів

def num(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def rule_src_1(table, tables, report, rule):
    publishers = set(v for v in table.col('publisher') if v)
    if len(publishers) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'різних видавців %d, а потрібно щонайменше два' % len(publishers))


def rule_src_2(table, tables, report, rule):
    urls = set(v for v in table.col('url') if v)
    if len(urls) < 3:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'різних документів %d, а потрібно щонайменше три' % len(urls))


def rule_src_3(table, tables, report, rule):
    urls = [v for v in table.col('url') if v]
    if urls and all('wikipedia.org' in u for u in urls):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі джерела ведуть на wikipedia.org, потрібен щонайменше один документ поза нею')


def rule_src_4(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        pub = parse_date(table.cell(row, 'pub_date'))
        acc = parse_date(table.cell(row, 'accessed'))
        if pub and acc and acc < pub:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'accessed %s раніша за pub_date %s' % (acc, pub))


CRITERIA = ['requirements', 'technology', 'release_cost', 'customer', 'contract', 'team']
LR02_CASES = ['C-1', 'C-2', 'C-3']
OPPOSITE = {'predictive': 'adaptive', 'adaptive': 'predictive'}
APPROACH_WORDS = {'predictive', 'adaptive', 'hybrid', 'предиктивний', 'адаптивний', 'гібрид',
                  'scrum', 'скрам', 'waterfall', 'kanban', 'канбан'}


def rule_ap_1(table, tables, report, rule):
    seen = {}
    for idx, row in enumerate(table.rows):
        key = (table.cell(row, 'case_id'), table.cell(row, 'criterion'))
        if not all(key):
            continue
        if key in seen:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'критерій `%s` для кейсу %s уже оцінений у рядку %d'
                       % (key[1], key[0], seen[key]))
        else:
            seen[key] = table.line(idx)


def rule_ap_2(table, tables, report, rule):
    by_case = {}
    for row in table.rows:
        case = table.cell(row, 'case_id')
        if case:
            by_case.setdefault(case, set()).add(table.cell(row, 'criterion'))
    for case in sorted(by_case):
        missing = [c for c in CRITERIA if c not in by_case[case]]
        if missing:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'у кейсі %s не оцінені критерії: %s' % (case, ', '.join(missing)))


def rule_ap_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        argument = table.cell(row, 'argument')
        if argument and len(argument) < 40:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'аргумент має %d символів: назвіть факт з опису кейсу, а не критерій'
                       % len(argument))


def rule_ap_4(table, tables, report, rule):
    by_case = {}
    for row in table.rows:
        case = table.cell(row, 'case_id')
        if case:
            by_case.setdefault(case, set()).add(table.cell(row, 'pull'))
    for case in sorted(by_case):
        if len(by_case[case]) < 2:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'усі критерії кейсу %s тягнуть в один бік: перевірте опис кейсу ще раз' % case)


def rule_ap_5(table, tables, report, rule):
    count = sum(1 for v in table.col('pull') if v == 'neutral')
    if count > 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків зі значенням neutral %d: перечитайте опис кейсу і назвіть бік' % count)


def rule_dc_1(table, tables, report, rule):
    present = set(v for v in table.col('case_id') if v)
    missing = [c for c in LR02_CASES if c not in present]
    if missing:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'немає рішення по кейсах: %s' % ', '.join(missing))


def rule_dc_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        for name in ('contract_impact', 'first_step'):
            value = table.cell(row, name)
            if not value:
                continue
            if len(value) < 40:
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'колонка `%s` має %d символів: цього замало для перевірюваного формулювання'
                           % (name, len(value)))
            elif value.strip().lower().strip('.') in APPROACH_WORDS:
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'колонка `%s` повторює назву підходу, а має описувати дію' % name)


def rule_dc_3(table, tables, report, rule):
    chosen = set(v for v in table.col('chosen_approach') if v)
    if len(chosen) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'на всі три кейси обрано один підхід (%s): кейси різні за задумом'
                   % ', '.join(sorted(chosen)))


def rule_st_1(table, tables, report, rule):
    expected = {('high', 'high'): 'manage_closely', ('low', 'high'): 'keep_satisfied',
                ('high', 'low'): 'keep_informed', ('low', 'low'): 'monitor'}
    for idx, row in enumerate(table.rows):
        key = (table.cell(row, 'interest'), table.cell(row, 'influence'))
        want = expected.get(key)
        got = table.cell(row, 'strategy')
        if want and got and want != got:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'інтерес %s і вплив %s дають стратегію %s, а стоїть %s'
                       % (key[0], key[1], want, got))


def rule_st_2(table, tables, report, rule):
    if 'manage_closely' not in table.col('strategy'):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жоден стейкхолдер не має стратегії manage_closely')


def rule_st_3(table, tables, report, rule):
    attitudes = set(v for v in table.col('attitude') if v)
    if attitudes and attitudes == {'supporter'}:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі стейкхолдери мають attitude supporter: у карті немає нікого, '
                   'кому проєкт заважає, і роботи з опором у ній не видно')


def rule_sc_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        target = table.cell(row, 'target')
        if target and not DIGIT_RE.search(target):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у target немає числа: «%s»' % target)


def rule_sc_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        how = table.cell(row, 'measure_how')
        target = table.cell(row, 'target')
        if not how:
            continue
        if how.strip().lower() == target.strip().lower():
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'measure_how повторює target: спосіб виміру не названий')
        elif len(how) < 20:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'measure_how надто короткий («%s»): назвіть джерело даних або момент виміру' % how)


def rule_sc_3(table, tables, report, rule):
    owners = set(v for v in table.col('accepted_by') if v)
    if len(owners) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі критерії приймає один стейкхолдер (%s)' % (', '.join(owners) or 'ніхто'))


def rule_bl_1(table, tables, report, rule):
    ranks = sorted(int(v) for v in table.col('rank') if INT_RE.match(v))
    expected = list(range(1, len(table.rows) + 1))
    if ranks != expected:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'rank має бути суцільним рядом від 1 до %d, а зараз %s'
                   % (len(table.rows), ranks or 'порожньо'))


def rule_bl_2(table, tables, report, rule):
    methods = set(v for v in table.col('priority_method') if v)
    if len(methods) > 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у файлі кілька методів пріоритезації: %s' % ', '.join(sorted(methods)))


def _first_release(table):
    """Рядки історій першого релізу разом із їхніми номерами в файлі."""
    return [(idx, row) for idx, row in enumerate(table.rows)
            if table.cell(row, 'release') == 'REL-1']


def rule_bl_3(table, tables, report, rule):
    for idx, row in _first_release(table):
        if not table.cell(row, 'acceptance_criteria'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s стоїть у першому релізі без критеріїв приймання'
                       % table.cell(row, 'story_id'))


def rule_bl_4(table, tables, report, rule):
    count = len(_first_release(table))
    if count < 8:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у релізі REL-1 %d історій, а потрібно щонайменше 8: '
                   'реліз меншого обсягу нічим не показати замовнику' % count)


def rule_bl_5(table, tables, report, rule):
    for idx, row in _first_release(table):
        if not table.cell(row, 'success_criterion'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s стоїть у першому релізі, але не веде до жодного критерію успіху'
                       % table.cell(row, 'story_id'))


PRIORITY_INPUTS = {
    'rice': ('reach', 'impact', 'confidence', 'effort'),
    'wsjf': ('business_value', 'time_criticality', 'risk_reduction', 'job_size'),
}


def _parse_inputs(value):
    parsed = {}
    for part in [p.strip() for p in value.split(';') if p.strip()]:
        if '=' not in part:
            return None
        name, raw = part.split('=', 1)
        if not NUMBER_RE.match(raw.strip()):
            return None
        parsed[name.strip()] = float(raw.strip())
    return parsed


def rule_bl_6(table, tables, report, rule):
    methods = set(v for v in table.col('priority_method') if v)
    method = methods.pop() if len(methods) == 1 else ''
    if method not in PRIORITY_INPUTS:
        return
    needed = PRIORITY_INPUTS[method]
    for idx, row in enumerate(table.rows):
        story = table.cell(row, 'story_id')
        raw = table.cell(row, 'priority_inputs')
        line = table.line(idx)
        if not raw:
            if table.cell(row, 'release') == 'REL-1':
                report.add(rule['severity'], table.path, line, rule['id'],
                           'історія %s стоїть у першому релізі з методом %s, '
                           'а priority_inputs порожній. Потрібні %s'
                           % (story, method, ', '.join(needed)))
            continue
        parsed = _parse_inputs(raw)
        if parsed is None:
            report.add(rule['severity'], table.path, line, rule['id'],
                       'історія %s: priority_inputs пишеться як `ключ=число` через `;`, а зараз «%s»'
                       % (story, raw))
            continue
        missing = [n for n in needed if n not in parsed]
        if missing:
            report.add(rule['severity'], table.path, line, rule['id'],
                       'історія %s: у priority_inputs немає складників %s' % (story, ', '.join(missing)))
            continue
        if method == 'rice':
            divisor = parsed['effort']
            expected = parsed['reach'] * parsed['impact'] * parsed['confidence'] / divisor if divisor else None
        else:
            divisor = parsed['job_size']
            expected = (parsed['business_value'] + parsed['time_criticality']
                        + parsed['risk_reduction']) / divisor if divisor else None
        if expected is None:
            report.add(rule['severity'], table.path, line, rule['id'],
                       'історія %s: дільник формули дорівнює нулю' % story)
            continue
        score = table.cell(row, 'priority_score')
        if not NUMBER_RE.match(score):
            report.add(rule['severity'], table.path, line, rule['id'],
                       'історія %s: метод %s дає число, а в priority_score стоїть «%s»'
                       % (story, method, score))
            continue
        if abs(float(score) - expected) > 0.1:
            report.add(rule['severity'], table.path, line, rule['id'],
                       'історія %s: за складниками формула дає %.2f, а в priority_score стоїть %s'
                       % (story, expected, score))


def rule_bl_7(table, tables, report, rule, enums=None):
    methods = set(v for v in table.col('priority_method') if v)
    if methods != {'moscow'}:
        return
    allowed = (enums or {}).get('moscow_class', ['must', 'should', 'could', 'wont'])
    for idx, row in enumerate(table.rows):
        score = table.cell(row, 'priority_score')
        if score and score not in allowed:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s: клас MoSCoW «%s» поза словником, дозволено: %s'
                       % (table.cell(row, 'story_id'), score, ', '.join(allowed)))
        elif score == 'wont' and table.cell(row, 'release') == 'REL-1':
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s має клас wont і водночас стоїть у першому релізі: '
                       'wont означає «не в цьому релізі»'
                       % table.cell(row, 'story_id'))


def rule_bl_8(table, tables, report, rule):
    ranks = [int(table.cell(row, 'rank')) for _, row in _first_release(table)
             if INT_RE.match(table.cell(row, 'rank'))]
    if not ranks:
        return
    last = max(ranks)
    for idx, row in enumerate(table.rows):
        release = table.cell(row, 'release')
        if release == 'REL-1':
            continue
        rank = table.cell(row, 'rank')
        if INT_RE.match(rank) and int(rank) < last:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s (%s) стоїть у черзі вище (rank %s) за історію першого релізу '
                       'з rank %d, хоча в перший реліз не входить'
                       % (table.cell(row, 'story_id'), release or 'без релізу', rank, last))


def rule_bl_9(table, tables, report, rule):
    if set(v for v in table.col('priority_method') if v) != {'moscow'}:
        return
    rows = _first_release(table)
    if not rows:
        return
    must = sum(1 for _, row in rows if table.cell(row, 'priority_score') == 'must')
    share = must * 100 // len(rows)
    if share > 60:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у першому релізі %d must із %d історій, це %d відсотків. '
                   'Коли строк почне тиснути, різати буде нічого'
                   % (must, len(rows), share))


def rule_bl_10(table, tables, report, rule):
    epics = set(v for v in table.col('epic') if v)
    if len(epics) < 3:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у беклозі %d епіків: %s. Беклог на 15 історій без трьох великих частин '
                   'зазвичай означає один епік, названий трьома словами'
                   % (len(epics), ', '.join(sorted(epics)) or 'жодного'))


def rule_bl_11(table, tables, report, rule):
    estimates = tables.get('lr08_poker/estimates.csv')
    if not filled(estimates):
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає заповненого estimates.csv, звірка розміру історій відкладена')
        return
    sized = {estimates.cell(row, 'story_id'): estimates.cell(row, 'final_estimate')
             for row in estimates.rows}
    for idx, row in _first_release(table):
        story = table.cell(row, 'story_id')
        if sized.get(story) == '21':
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s оцінена в 21 point: у спринт вона не вміщується, її треба різати'
                       % story)


def rule_rm_1(table, tables, report, rule):
    backlog = tables.get('lr06_backlog/backlog.csv')
    if not filled(backlog):
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає заповненого backlog.csv, склад релізів не перевірений')
        return
    planned = set(v for v in backlog.col('release') if v)
    for idx, row in enumerate(table.rows):
        release = table.cell(row, 'release_id')
        if release and release not in planned:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у реліз %s не запланована жодна історія беклогу' % release)


def rule_wbs_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        wbs_id = table.cell(row, 'wbs_id')
        level = table.cell(row, 'level')
        if wbs_id and INT_RE.match(level) and int(level) != len(wbs_id.split('.')):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у ключі `%s` %d рівнів, а в колонці level стоїть %s'
                       % (wbs_id, len(wbs_id.split('.')), level))


def rule_wbs_2(table, tables, report, rule):
    ids = set(v for v in table.col('wbs_id') if v)
    for idx, row in enumerate(table.rows):
        parent = table.cell(row, 'parent_id')
        wbs_id = table.cell(row, 'wbs_id')
        if not parent:
            continue
        if parent not in ids:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'батьківського вузла `%s` немає у файлі' % parent)
        elif not wbs_id.startswith(parent + '.'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'ключ `%s` не є нащадком `%s`' % (wbs_id, parent))


def rule_wbs_3(table, tables, report, rule):
    hours = {}
    children = {}
    for idx, row in enumerate(table.rows):
        wbs_id = table.cell(row, 'wbs_id')
        hours[wbs_id] = num(table.cell(row, 'estimate_hours'))
        parent = table.cell(row, 'parent_id')
        if parent:
            children.setdefault(parent, []).append(wbs_id)
    for idx, row in enumerate(table.rows):
        wbs_id = table.cell(row, 'wbs_id')
        kids = children.get(wbs_id)
        if not kids:
            continue
        total = sum(hours.get(k, 0.0) for k in kids)
        if abs(total - hours.get(wbs_id, 0.0)) > 0.01:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'правило 100%%: вузол `%s` має %s годин, а сума дітей %s'
                       % (wbs_id, hours.get(wbs_id, 0.0), total))


# ------------------------------------------------------- календарний план ЛР7

def _schedule_graph(table):
    """Читає schedule.csv у структуру для розрахунку критичного шляху."""
    tasks = {}
    order = []
    for idx, row in enumerate(table.rows):
        task_id = table.cell(row, 'task_id')
        if not task_id:
            continue
        preds = [p.strip() for p in table.cell(row, 'predecessors').split(';') if p.strip()]
        tasks[task_id] = {
            'line': table.line(idx),
            'duration': int(num(table.cell(row, 'duration_days'))),
            'preds': preds,
        }
        order.append(task_id)
    return tasks, order


def _topological(tasks, order):
    """Порядок обходу графа. Повертає None, якщо в залежностях є цикл."""
    state = {}
    result = []

    def visit(node, stack):
        if state.get(node) == 'done':
            return True
        if node in stack:
            return False
        stack.add(node)
        for pred in tasks[node]['preds']:
            if pred not in tasks:
                continue
            if not visit(pred, stack):
                return False
        stack.discard(node)
        state[node] = 'done'
        result.append(node)
        return True

    for node in order:
        if not visit(node, set()):
            return None
    return result


def _critical_path(tasks, order):
    """Прямий і зворотний проходи CPM. Повертає резерв кожної роботи в днях."""
    seq = _topological(tasks, order)
    if seq is None:
        return None
    early_start, early_finish = {}, {}
    for node in seq:
        preds = [p for p in tasks[node]['preds'] if p in tasks]
        early_start[node] = max([early_finish[p] for p in preds], default=0)
        early_finish[node] = early_start[node] + tasks[node]['duration']
    finish = max(early_finish.values(), default=0)
    successors = {node: [] for node in tasks}
    for node in tasks:
        for pred in tasks[node]['preds']:
            if pred in successors:
                successors[pred].append(node)
    late_start, late_finish = {}, {}
    for node in reversed(seq):
        nexts = successors[node]
        late_finish[node] = min([late_start[n] for n in nexts], default=finish)
        late_start[node] = late_finish[node] - tasks[node]['duration']
    return dict((node, late_start[node] - early_start[node]) for node in tasks)


def rule_sch_1(table, tables, report, rule):
    tasks, order = _schedule_graph(table)
    if _topological(tasks, order) is None:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у залежностях є цикл: робота через ланцюг попередників чекає сама на себе, '
                   'критичний шлях у такому графі не рахується')


def rule_sch_2(table, tables, report, rule):
    tasks, order = _schedule_graph(table)
    floats = _critical_path(tasks, order)
    if floats is None:
        return
    for idx, row in enumerate(table.rows):
        task_id = table.cell(row, 'task_id')
        if task_id not in floats:
            continue
        declared = table.cell(row, 'float_days')
        if not INT_RE.match(declared):
            continue
        if int(declared) != floats[task_id]:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'робота %s: у файлі резерв %s днів, а за тривалостями і залежностями %d'
                       % (task_id, declared, floats[task_id]))


def rule_sch_3(table, tables, report, rule):
    tasks, order = _schedule_graph(table)
    floats = _critical_path(tasks, order)
    if floats is None:
        return
    for idx, row in enumerate(table.rows):
        task_id = table.cell(row, 'task_id')
        if task_id not in floats:
            continue
        marked = table.cell(row, 'is_critical') == 'yes'
        critical = floats[task_id] == 0
        if marked != critical:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'робота %s: is_critical=%s, а резерв %d днів'
                       % (task_id, table.cell(row, 'is_critical'), floats[task_id]))


def rule_sch_4(table, tables, report, rule):
    count = sum(1 for row in table.rows if table.cell(row, 'milestone') == 'yes')
    if count < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'віх у плані %d: контрольних точок замало, щоб побачити зрив строку вчасно' % count)


def rule_sch_5(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'milestone') != 'yes':
            continue
        if num(table.cell(row, 'duration_days')) != 0:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'віха %s має тривалість %s: віха це подія нульової тривалості, '
                       'роботу перед нею винесіть окремим рядком'
                       % (table.cell(row, 'task_id'), table.cell(row, 'duration_days')))


def rule_rm_3(table, tables, report, rule):
    pairs = []
    for idx, row in enumerate(table.rows):
        date = parse_date(table.cell(row, 'target_date'))
        if date:
            pairs.append((table.cell(row, 'release_id'), date, table.line(idx)))
    for prev, cur in zip(pairs, pairs[1:]):
        if cur[1] < prev[1]:
            report.add(rule['severity'], table.path, cur[2], rule['id'],
                       'реліз %s датований раніше за попередній %s' % (cur[0], prev[0]))


POINT_SCALE = [0, 1, 2, 3, 5, 8, 13, 21]
THREE_POINT_VOTERS = ('optimistic', 'likely', 'pessimistic')


def vote_rounds(votes):
    """Голоси ЛР8 у вигляді {історія: {раунд: {голосуючий: картка}}}."""
    grouped = {}
    for row in votes.rows:
        story = votes.cell(row, 'story_id')
        rnd = votes.cell(row, 'round')
        if not story or not INT_RE.match(rnd):
            continue
        voter = votes.cell(row, 'voter').strip().lower()
        grouped.setdefault(story, {}).setdefault(int(rnd), {})[voter] = votes.cell(row, 'vote').strip()
    return grouped


def pert_value(round_votes):
    """PERT за трьома картками раунду. None, якщо хоч одна не число."""
    numbers = []
    for name in THREE_POINT_VOTERS:
        value = round_votes.get(name, '')
        if not INT_RE.match(value):
            return None
        numbers.append(int(value))
    return (numbers[0] + 4 * numbers[1] + numbers[2]) / 6.0


def nearest_cards(value):
    """Картки шкали, найближчі до числа. Дві, якщо число рівно посередині."""
    distances = [round(abs(card - value), 6) for card in POINT_SCALE]
    best = min(distances)
    return [card for card, distance in zip(POINT_SCALE, distances) if distance == best]


def scale_index(value):
    if INT_RE.match(value) and int(value) in POINT_SCALE:
        return POINT_SCALE.index(int(value))
    return None


def rule_pk_4(table, tables, report, rule):
    for story, rounds in sorted(vote_rounds(table).items()):
        for number, votes in sorted(rounds.items()):
            names = set(votes)
            if not names & set(THREE_POINT_VOTERS):
                continue
            if names != set(THREE_POINT_VOTERS):
                report.add(rule['severity'], table.path, 1, rule['id'],
                           'історія %s, раунд %d: у триточковому раунді рівно три голоси, '
                           'optimistic, likely і pessimistic, а тут %s'
                           % (story, number, ', '.join(sorted(names))))
                continue
            order = [scale_index(votes[name]) for name in THREE_POINT_VOTERS]
            if None in order:
                continue
            if not order[0] <= order[1] <= order[2]:
                report.add(rule['severity'], table.path, 1, rule['id'],
                           'історія %s, раунд %d: оптимістична %s, найімовірніша %s, песимістична %s, '
                           'а має бути від меншого до більшого'
                           % (story, number, votes['optimistic'], votes['likely'], votes['pessimistic']))


def rule_pk_5(table, tables, report, rule):
    for story, rounds in sorted(vote_rounds(table).items()):
        last = max(rounds)
        if '?' in rounds[last].values():
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'історія %s: в останньому раунді %d стоїть картка ?, '
                       'підсумкову оцінку нема з чого рахувати' % (story, last))


def rule_pk_1(table, tables, report, rule):
    seen = {}
    for idx, row in enumerate(table.rows):
        key = (table.cell(row, 'story_id'), table.cell(row, 'round'), table.cell(row, 'voter'))
        if key in seen:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       '%s уже голосував за %s у раунді %s, рядок %d' % (key[2], key[0], key[1], seen[key]))
        else:
            seen[key] = table.line(idx)


def rule_pk_3(table, tables, report, rule):
    counts = {}
    for row in table.rows:
        key = (table.cell(row, 'story_id'), table.cell(row, 'round'))
        counts[key] = counts.get(key, 0) + 1
    for (story, rnd), count in sorted(counts.items()):
        if count < 3:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'історія %s, раунд %s: голосів %d, а сесія командна' % (story, rnd, count))


def rule_es_1(table, tables, report, rule):
    votes = tables.get('lr08_poker/votes.csv')
    if votes is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає votes.csv, звірка раундів відкладена')
        return
    max_round = {}
    for row in votes.rows:
        story = votes.cell(row, 'story_id')
        value = votes.cell(row, 'round')
        if INT_RE.match(value):
            max_round[story] = max(max_round.get(story, 0), int(value))
    for idx, row in enumerate(table.rows):
        story = table.cell(row, 'story_id')
        rounds = table.cell(row, 'rounds')
        if story in max_round and INT_RE.match(rounds) and int(rounds) != max_round[story]:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'для %s у votes.csv максимальний раунд %d, а тут стоїть %s'
                       % (story, max_round[story], rounds))


def rule_es_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        rounds = table.cell(row, 'rounds')
        if INT_RE.match(rounds) and int(rounds) > 1 and not table.cell(row, 'spread_note'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'раундів %s, а причина розкиду в spread_note не написана' % rounds)


def rule_es_3(table, tables, report, rule):
    votes = tables.get('lr08_poker/votes.csv')
    if votes is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає votes.csv, звірка складу історій відкладена')
        return
    estimated = set(v for v in table.col('story_id') if v)
    for story in sorted(set(v for v in votes.col('story_id') if v)):
        if story not in estimated:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'історія %s голосувалася, але рядка в estimates.csv не має' % story)


def rule_es_4(table, tables, report, rule):
    votes = tables.get('lr08_poker/votes.csv')
    if votes is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає votes.csv, звірка формули відкладена')
        return
    grouped = vote_rounds(votes)
    for idx, row in enumerate(table.rows):
        story = table.cell(row, 'story_id')
        rounds = grouped.get(story)
        if not rounds:
            continue
        last = rounds[max(rounds)]
        if set(last) != set(THREE_POINT_VOTERS):
            continue
        value = pert_value(last)
        final = table.cell(row, 'final_estimate')
        if value is None or scale_index(final) is None:
            continue
        cards = nearest_cards(value)
        if int(final) not in cards:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'для %s PERT дає %.2f, найближча картка %s, а у файлі %s'
                       % (story, value, ' або '.join(str(c) for c in cards), final))


def rule_es_5(table, tables, report, rule):
    votes = tables.get('lr08_poker/votes.csv')
    if votes is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає votes.csv, звірка голосів відкладена')
        return
    voted = set(v for v in votes.col('story_id') if v)
    for idx, row in enumerate(table.rows):
        story = table.cell(row, 'story_id')
        if story and story not in voted:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'історія %s має підсумкову оцінку, але в votes.csv за неї ніхто не голосував' % story)


def rule_es_6(table, tables, report, rule):
    votes = tables.get('lr08_poker/votes.csv')
    if votes is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає votes.csv, звірка розкиду відкладена')
        return
    grouped = vote_rounds(votes)
    for idx, row in enumerate(table.rows):
        story = table.cell(row, 'story_id')
        rounds = grouped.get(story)
        if not rounds:
            continue
        last = rounds[max(rounds)]
        order = [scale_index(v) for v in last.values()]
        order = [v for v in order if v is not None]
        if len(order) < 2 or max(order) - min(order) <= 2:
            continue
        if not table.cell(row, 'spread_note').strip():
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'для %s крайні голоси %s і %s різняться більш ніж на дві картки шкали, '
                       'а spread_note порожній'
                       % (story, POINT_SCALE[min(order)], POINT_SCALE[max(order)]))


def rule_vl_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        start = parse_date(table.cell(row, 'start_date'))
        end = parse_date(table.cell(row, 'end_date'))
        if start and end and end <= start:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'end_date %s не пізніша за start_date %s' % (end, start))


def rule_vl_2(table, tables, report, rule):
    sprints = sorted(int(v) for v in table.col('sprint') if INT_RE.match(v))
    if sprints and sprints != list(range(sprints[0], sprints[0] + len(sprints))):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'номери спринтів мають іти підряд, а зараз %s' % sprints)


def rule_vl_3(table, tables, report, rule):
    spans = []
    for idx, row in enumerate(table.rows):
        sprint = table.cell(row, 'sprint')
        start = parse_date(table.cell(row, 'start_date'))
        end = parse_date(table.cell(row, 'end_date'))
        if start and end and INT_RE.match(sprint):
            spans.append((int(sprint), start, end, idx))
    spans.sort()
    for prev, cur in zip(spans, spans[1:]):
        if cur[1] < prev[2]:
            report.add(rule['severity'], table.path, table.line(cur[3]), rule['id'],
                       'спринт %d починається %s, а спринт %d ще триває до %s'
                       % (cur[0], cur[1], prev[0], prev[2]))


def rule_vl_4(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        start = parse_date(table.cell(row, 'start_date'))
        end = parse_date(table.cell(row, 'end_date'))
        if not (start and end):
            continue
        days = (end - start).days
        if days < 7 or days > 21:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'спринт %s: тривалість %d дн., а має бути від 7 до 21'
                       % (table.cell(row, 'sprint'), days))


def rule_fc_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        p50 = num(table.cell(row, 'p50_sprints'), None)
        p85 = num(table.cell(row, 'p85_sprints'), None)
        if p50 is not None and p85 is not None and p85 < p50:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'p85_sprints %s менше за p50_sprints %s' % (p85, p50))
        d50 = parse_date(table.cell(row, 'p50_date'))
        d85 = parse_date(table.cell(row, 'p85_date'))
        if d50 and d85 and d85 < d50:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'p85_date %s раніша за p50_date %s' % (d85, d50))


def rule_fc_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        p50 = num(table.cell(row, 'p50_sprints'), None)
        p85 = num(table.cell(row, 'p85_sprints'), None)
        d50 = parse_date(table.cell(row, 'p50_date'))
        d85 = parse_date(table.cell(row, 'p85_date'))
        if p50 is None or p85 is None or not (d50 and d85):
            continue
        expected = (p85 - p50) * 14
        actual = (d85 - d50).days
        if abs(actual - expected) > 1:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'між p50_date і p85_date %d дн., а різниця p85_sprints і p50_sprints це %g, тобто %.0f дн.'
                       % (actual, p85 - p50, expected))


def rule_fc_2(table, tables, report, rule):
    """Звірка обсягу сценарію з оцінками.

    Спека не задає, які історії входять у сценарій, тому механічно перевіряється
    те, що піддається перевірці: сценарій не може містити більше очок, ніж
    оцінено взагалі, і щонайменше один сценарій має покривати весь оцінений обсяг.
    """
    estimates = tables.get('lr08_poker/estimates.csv')
    if estimates is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає estimates.csv, звірка обсягу відкладена')
        return
    total = sum(num(v) for v in estimates.col('final_estimate') if v)
    matched = False
    for idx, row in enumerate(table.rows):
        remaining = num(table.cell(row, 'remaining_points'))
        if remaining > total + 0.01:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'сценарій «%s» має %s очок, а весь оцінений беклог це %s'
                       % (table.cell(row, 'scenario'), remaining, total))
        if abs(remaining - total) <= 0.01:
            matched = True
    if not matched:
        report.add(WARNING, table.path, 1, rule['id'],
                   'жоден сценарій не дорівнює сумі оцінок (%s): перевірте, який обсяг ви прогнозуєте' % total)


def rule_rk_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        p, i, s = (table.cell(row, 'probability'), table.cell(row, 'impact'), table.cell(row, 'score'))
        if INT_RE.match(p) and INT_RE.match(i) and INT_RE.match(s) and int(p) * int(i) != int(s):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'score має дорівнювати %d, а стоїть %s' % (int(p) * int(i), s))


def rule_rk_2(table, tables, report, rule, enums=None):
    present = set(v for v in table.col('category') if v)
    missing = [c for c in ('technical', 'external', 'organizational', 'project_management')
               if c not in present]
    if missing:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у реєстрі немає ризиків категорій: %s' % ', '.join(missing))


def rule_rk_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        owner = table.cell(row, 'owner')
        if owner.strip().lower() in TEAM_OWNER_WORDS:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'власник `%s` це не людина: ризик без імені не має власника' % owner)


def rule_td_1(table, tables, report, rule):
    if 'deliberate_prudent' not in table.col('type'):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'немає жодного запису типу deliberate_prudent')


def rule_rk_5(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        prev = table.cell(row, 'previous_score')
        if not prev:
            continue
        score = table.cell(row, 'score')
        if INT_RE.match(prev) and INT_RE.match(score) and int(prev) == int(score):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'previous_score дорівнює score: оцінка не змінилась, лишіть колонку порожньою')
        elif not table.cell(row, 'review_note'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'оцінка змінилась, а review_note порожній: незрозуміло, що саме сталося')


def rule_rk_6(table, tables, report, rule):
    changed = sum(1 for row in table.rows if table.cell(row, 'previous_score'))
    if changed < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'переоцінених ризиків %d: реєстр, у якому після спринта не змінилось нічого, '
                   'найчастіше не переглядали' % changed)


def rule_td_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        decision = table.cell(row, 'decision')
        due = table.cell(row, 'due_sprint')
        if decision in ('pay_now', 'pay_later') and not due:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'рішення `%s` без due_sprint: «потім» без дати не настає ніколи' % decision)
        if decision == 'accept' and due:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'рішення accept зі спринтом %s: або гасимо за планом, або свідомо не гасимо' % due)


def rule_ch_1(table, tables, report, rule):
    if 'course_event' not in table.col('source'):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у журналі немає жодного рядка з source course_event: подія курсу не відпрацьована')


def rule_ch_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'decision') != 'accepted':
            continue
        for name in ('points_delta', 'affected_files'):
            if not table.cell(row, name):
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'зміна прийнята, а `%s` порожній: прийнята зміна міняє числа і файли портфеля'
                           % name)


def rule_ch_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'decision') == 'accepted' and not table.cell(row, 'story_ids'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'прийнята зміна не назвала жодної історії беклогу')


def rule_rc_1(table, tables, report, rule):
    seen = {}
    for idx, row in enumerate(table.rows):
        key = (table.cell(row, 'activity_id'), table.cell(row, 'stakeholder_id'))
        if key in seen:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'пара %s і %s уже є в рядку %d' % (key[0], key[1], seen[key]))
        else:
            seen[key] = table.line(idx)


def _by_activity(table):
    groups = {}
    for idx, row in enumerate(table.rows):
        groups.setdefault(table.cell(row, 'activity_id'), []).append((idx, row))
    return groups


def rule_rc_2(table, tables, report, rule):
    for activity, rows in sorted(_by_activity(table).items()):
        count = sum(1 for _, row in rows if table.cell(row, 'role') in ('A', 'AR'))
        if count != 1:
            report.add(rule['severity'], table.path, table.line(rows[0][0]), rule['id'],
                       'активність %s має %d ролей A, а має бути рівно одна' % (activity, count))


def rule_rc_3(table, tables, report, rule):
    for activity, rows in sorted(_by_activity(table).items()):
        if not any(table.cell(row, 'role') in ('R', 'AR') for _, row in rows):
            report.add(rule['severity'], table.path, table.line(rows[0][0]), rule['id'],
                       'активність %s не має жодної ролі R: роботу ніхто не виконує' % activity)


def rule_rc_4(table, tables, report, rule):
    for activity, rows in sorted(_by_activity(table).items()):
        texts = set(table.cell(row, 'activity') for _, row in rows)
        if len(texts) > 1:
            report.add(rule['severity'], table.path, table.line(rows[0][0]), rule['id'],
                       'активність %s називається по-різному: %s' % (activity, '; '.join(sorted(texts))))


def rule_rc_5(table, tables, report, rule):
    count = len(_by_activity(table))
    if count < 6:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'активностей %d, а за правилом щонайменше шість' % count)


def rule_rc_6(table, tables, report, rule):
    for activity, rows in sorted(_by_activity(table).items()):
        values = set(table.cell(row, 'wbs_id') for _, row in rows)
        if len(values) > 1:
            report.add(rule['severity'], table.path, table.line(rows[0][0]), rule['id'],
                       'активність %s посилається на різні вузли WBS: %s'
                       % (activity, '; '.join(sorted(v or '(порожньо)' for v in values))))


def rule_rc_7(table, tables, report, rule):
    linked = sum(1 for _, rows in _by_activity(table).items()
                 if any(table.cell(row, 'wbs_id') for _, row in rows))
    if linked < 4:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'на вузли WBS посилаються %d активності, а за правилом щонайменше чотири' % linked)


def rule_rc_9(table, tables, report, rule):
    free = sum(1 for _, rows in _by_activity(table).items()
               if not any(table.cell(row, 'wbs_id') for _, row in rows))
    if free < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'активностей без вузла WBS %d, а за правилом щонайменше дві: '
                   'у матриці немає рішень, тільки роботи' % free)


def rule_rc_8(table, tables, report, rule):
    holders = set(table.cell(row, 'stakeholder_id') for row in table.rows
                  if table.cell(row, 'role') in ('A', 'AR'))
    holders.discard('')
    if len(holders) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'роль A стоїть тільки в %d сторони: у матриці немає жодного спірного призначення'
                   % len(holders))


def rule_cm_1(table, tables, report, rule):
    stakeholders = tables.get('lr05_charter/stakeholders.csv')
    if stakeholders is None:
        report.add(SKIPPED, table.path, 1, rule['id'], 'немає stakeholders.csv, перевірка відкладена')
        return
    covered = set(v for v in table.col('stakeholder_id') if v)
    for idx, row in enumerate(stakeholders.rows):
        strategy = stakeholders.cell(row, 'strategy')
        sid = stakeholders.cell(row, 'stakeholder_id')
        if strategy in ('manage_closely', 'keep_satisfied') and sid not in covered:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'стейкхолдер %s зі стратегією %s не має жодного рядка комунікацій' % (sid, strategy))


def rule_cm_2(table, tables, report, rule):
    if not any(table.cell(row, 'frequency') == 'on_event' for row in table.rows):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у плані немає жодного рядка з частотою on_event: '
                   'погана новина не має каналу і чекатиме планового звіту')


REGULAR_FREQUENCY = ('daily', 'weekly', 'biweekly', 'monthly')


def rule_cm_3(table, tables, report, rule):
    if not any(table.cell(row, 'frequency') in REGULAR_FREQUENCY for row in table.rows):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у плані немає жодного регулярного рядка: ритму, за яким вас чекають, немає')


def rule_fl_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        created = parse_date(table.cell(row, 'created_date'))
        start = parse_date(table.cell(row, 'start_date'))
        done = parse_date(table.cell(row, 'done_date'))
        if created and start and created > start:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'created_date %s пізніша за start_date %s' % (created, start))
        if start and done and start > done:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'start_date %s пізніша за done_date %s' % (start, done))


def rule_fl_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        start = parse_date(table.cell(row, 'start_date'))
        done = parse_date(table.cell(row, 'done_date'))
        blocked = table.cell(row, 'blocked_days')
        if start and done and INT_RE.match(blocked):
            span = (done - start).days
            if int(blocked) > span:
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'blocked_days %s більше за %d днів між start_date і done_date' % (blocked, span))


def rule_fl_3(table, tables, report, rule):
    empty_ids = [row for row in table.rows if not table.cell(row, 'story_id')]
    if len(empty_ids) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'карток без story_id %d: завершені картки дошки ЛР4 у файл не потрапили'
                   % len(empty_ids))


def rule_fl_4(table, tables, report, rule):
    filled_ids = [v for v in table.col('story_id') if v]
    if len(filled_ids) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'карток з story_id %d: історії двох спринтів у файл не потрапили'
                   % len(filled_ids))


def rule_fl_5(table, tables, report, rule):
    same = 0
    counted = 0
    for row in table.rows:
        start = parse_date(table.cell(row, 'start_date'))
        done = parse_date(table.cell(row, 'done_date'))
        if not start or not done:
            continue
        counted += 1
        if start == done:
            same += 1
    if counted and same == counted:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі %d карток взяті і закриті того самого дня: cycle time скрізь нуль, '
                   'і перцентиль рахувати нема на чому' % counted)


REFERENCE_DATASETS = ('burndown', 'cfd', 'dora')
WATCH_WORDS = ('стеж', 'моніто', 'контролюва', 'слідкув', 'тримати на контролі',
               'спостеріга', 'наглядати')


def rule_fd_1(table, tables, report, rule):
    for name in REFERENCE_DATASETS:
        count = sum(1 for v in table.col('dataset') if v == name)
        if count < 2:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'знахідок по датасету %s лише %d, а потрібно щонайменше дві'
                       % (name, count))


def rule_fd_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        evidence = table.cell(row, 'evidence')
        if evidence and not DIGIT_RE.search(evidence):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у доказі знахідки %s немає жодного числа з даних'
                       % table.cell(row, 'finding_id'))


def rule_fd_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        action = table.cell(row, 'action').lower()
        if any(word in action for word in WATCH_WORDS):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'дія за знахідкою %s це намір спостерігати, а не дія'
                       % table.cell(row, 'finding_id'))


def rule_db_1(table, tables, report, rule):
    if len(table.rows) > 5:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'метрик на дашборді %d, а має бути від трьох до п\'яти' % len(table.rows))


def rule_db_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        value = table.cell(row, 'value')
        if value and not DIGIT_RE.search(value):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'значення метрики %s не містить числа' % table.cell(row, 'metric_id'))


def rule_db_3(table, tables, report, rule):
    if not any('flow.csv' in v for v in table.col('source_file')):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жодна метрика не береться з lr14_metrics/flow.csv: '
                   'на дашборді немає власного потоку')


OBSERVATION_WORDS = ('проаналізува', 'звернути увагу', 'розібрат', 'взяти до уваги',
                     'переглянути ситуац', 'подивит', 'оцінити ситуац')


def rule_db_4(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        threshold = table.cell(row, 'threshold')
        if threshold and not DIGIT_RE.search(threshold):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'межа метрики %s не містить числа: перетнути її неможливо'
                       % table.cell(row, 'metric_id'))


def rule_db_5(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        decision = table.cell(row, 'decision').lower()
        if any(word in decision for word in OBSERVATION_WORDS):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'рішення за метрикою %s це спостереження, а не дія'
                       % table.cell(row, 'metric_id'))


VAGUE_DELTA_WORDS = ('незначн', 'суттєв', 'мінімальн', 'великий', 'велика', 'велике',
                     'помірн', 'невелик', 'значн', 'критичн', 'низьк', 'висок')


def _impact_row(table, name):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'dimension') == name:
            return idx, row
    return None, None


def rule_im_1(table, tables, report, rule):
    for name in ('scope', 'schedule', 'budget'):
        idx, row = _impact_row(table, name)
        if row is None:
            continue
        for field in ('before', 'after'):
            value = table.cell(row, field)
            if value and not DIGIT_RE.search(value):
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'у вимірі %s поле `%s` не містить числа: '
                           'обсяг, строк і бюджет міряються числом' % (name, field))


def rule_im_2(table, tables, report, rule):
    idx, row = _impact_row(table, 'risk')
    if row is None:
        return
    if not table.cell(row, 'risk_ids').strip():
        report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                   'у вимірі risk порожній risk_ids: зміна, яка не зачепила жодного '
                   'рядка реєстру ризиків, не оцінена')


def rule_im_3(table, tables, report, rule):
    moved = sum(1 for row in table.rows
                if table.cell(row, 'before').strip() != table.cell(row, 'after').strip())
    if moved < 3:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків, у яких before і after різні, %d, а потрібно щонайменше три: '
                   'зміна, після якої в портфелі нічого не рухається, зміною не є' % moved)


def rule_im_4(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        delta = table.cell(row, 'delta')
        if not delta or DIGIT_RE.search(delta):
            continue
        low = delta.lower()
        if any(word in low for word in VAGUE_DELTA_WORDS):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'різниця у вимірі %s названа оцінним словом без числа'
                       % table.cell(row, 'dimension'))


def rule_bg_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        hours = table.cell(row, 'hours')
        rate = table.cell(row, 'rate')
        amount = table.cell(row, 'amount')
        if hours and rate and NUMBER_RE.match(hours) and NUMBER_RE.match(rate) and NUMBER_RE.match(amount):
            expected = round(float(hours) * float(rate), 2)
            if abs(expected - float(amount)) > 0.01:
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'hours × rate дає %s, а в amount стоїть %s' % (expected, amount))


def rule_bg_2(table, tables, report, rule):
    count = sum(1 for v in table.col('category') if v == 'contingency')
    if count != 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків категорії contingency %d, а має бути рівно один' % count)


def rule_bg_3(table, tables, report, rule):
    count = sum(1 for v in table.col('category') if v == 'management_reserve')
    if count != 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків категорії management_reserve %d, а має бути рівно один: '
                   'резерв на невідоме це окремий блок рубрики' % count)


def rule_bg_5(table, tables, report, rule):
    count = sum(1 for v in table.col('category') if v == 'labor')
    if count < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків категорії labor %d: кошторис однієї ролі це не кошторис команди' % count)


def rule_bg_6(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'category') != 'labor':
            continue
        hours = table.cell(row, 'hours')
        rate = table.cell(row, 'rate')
        if not hours or not rate:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у трудового рядка заповнена не вся пара hours і rate: рядок з однією '
                       'сумою обходить і звірку добутку, і звірку з прайсом')


def rule_bg_7(table, tables, report, rule):
    direct = ('tools', 'infrastructure', 'other')
    if not any(v in direct for v in table.col('category')):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у кошторисі немає жодного рядка нетрудових прямих витрат: ліцензії, хмара '
                   'і середовища коштують грошей на будь-якому проєкті')


def rule_rt_1(table, tables, report, rule):
    rates = set(table.cell(row, 'rate') for row in table.rows if table.cell(row, 'rate'))
    if len(rates) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі ролі коштують однаково: прайс, у якому PM і Dev мають ту саму ставку, '
                   'не є rate card')


def rule_rt_2(table, tables, report, rule):
    hours = sum(num(table.cell(row, 'hours')) for row in table.rows)
    money = sum(num(table.cell(row, 'hours')) * num(table.cell(row, 'rate'))
                for row in table.rows)
    if hours <= 0:
        return
    blended = money / hours
    if blended < 400 or blended > 600:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'blended rate %.0f грн за годину, а таблиця варіантів тримає його '
                   'від 400 до 600: перерахуйте ставки під бюджет свого варіанта' % blended)


def rule_rt_3(table, tables, report, rule):
    if not any('http' in table.cell(row, 'source') for row in table.rows):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у жодному рядку немає посилання на огляд зарплат: ставка без джерела '
                   'це ставка зі стелі')


def _pf_pair(table, row):
    return (num(table.cell(row, 'planned_points')),
            num(table.cell(row, 'actual_points')),
            num(table.cell(row, 'planned_cost_per_point')),
            num(table.cell(row, 'actual_cost_per_point')))


def rule_pf_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        pp, ap, pc, ac = _pf_pair(table, row)
        if min(pp, ap, pc, ac) <= 0:
            continue
        planned, actual = pp * pc, ap * ac
        if abs(planned - actual) > planned * 0.01:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'план дає вартість спринта %.0f грн, а факт %.0f: обидва добутки це '
                       'вартість того самого спринта і мають збігатися' % (planned, actual))


def rule_pf_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        pp, ap, pc, ac = _pf_pair(table, row)
        stated = table.cell(row, 'variance_pct')
        if pc <= 0 or not NUMBER_RE.match(stated):
            continue
        expected = (ac - pc) / pc * 100
        if abs(expected - float(stated)) > 0.5:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'відхилення за формулою %.1f відсотка, а в variance_pct стоїть %s'
                       % (expected, stated))


def rule_pf_3(table, tables, report, rule):
    planned = set(table.cell(row, 'planned_cost_per_point') for row in table.rows
                  if table.cell(row, 'planned_cost_per_point'))
    if len(planned) > 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'планова ціна points різна в спринтах (%s): вона береться з варіанта '
                   'і між спринтами не змінюється' % ', '.join(sorted(planned)))


def rule_pf_4(table, tables, report, rule):
    if not table.rows:
        return
    same = all(num(table.cell(row, 'planned_points')) == num(table.cell(row, 'actual_points'))
               for row in table.rows)
    if same:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'факт дорівнює плану в обох спринтах: так буває, але частіше це числа, '
                   'підігнані під нульове відхилення')


def rule_bg_4(table, tables, report, rule):
    row = next((r for r in table.rows if table.cell(r, 'category') == 'contingency'), None)
    if row is None:
        return
    line = table.line(table.rows.index(row))
    percent = PERCENT_RE.search(table.cell(row, 'note'))
    if not percent:
        report.add(rule['severity'], table.path, line, rule['id'],
                   'у note рядка contingency немає відсотка: без нього суму резерву '
                   'неможливо перевірити')
        return
    direct = sum(num(table.cell(r, 'amount')) for r in table.rows
                 if table.cell(r, 'category') not in ('contingency', 'management_reserve'))
    if direct <= 0:
        return
    expected = direct * float(percent.group(1).replace(',', '.')) / 100
    actual = num(table.cell(row, 'amount'))
    if abs(expected - actual) > max(expected * 0.01, 1):
        report.add(rule['severity'], table.path, line, rule['id'],
                   'названий відсоток дає %.0f грн від прямих витрат %.0f, а в amount стоїть %.0f'
                   % (expected, direct, actual))


# ------------------------------------------------------------------ ЛР17, AI

def _count(table, name, value):
    return sum(1 for v in table.col(name) if v == value)


def rule_tk_1(table, tables, report, rule):
    for value in ('mine', 'ai'):
        if _count(table, 'origin', value) == 0:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'жодного рядка з origin %s: у файлі одна версія переліку, а не дві' % value)


def rule_tk_2(table, tables, report, rule):
    count = _count(table, 'type', 'open_question')
    if count < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'відкритих питань %d, а рядків «Питання без відповіді» у нотатках два' % count)


def rule_tk_3(table, tables, report, rule):
    count = _count(table, 'type', 'task')
    if count < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'задач у переліку %d: розбір нотаток без задач це не розбір' % count)


def rule_tk_4(table, tables, report, rule):
    tasks = [row for row in table.rows if table.cell(row, 'type') == 'task']
    if tasks and all(table.cell(row, 'owner') for row in tasks):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у кожної задачі стоїть відповідальний, хоча в нотатках названі не всі: '
                   'перевірте, чи не дописав їх інструмент')


def rule_tk_5(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if table.cell(row, 'origin') in ('mine', 'ai') and not table.cell(row, 'note'):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у пункту %s не сказано, чим він відрізняється у двох версіях'
                       % table.cell(row, 'item_id'))


def rule_pr_1(table, tables, report, rule):
    count = _count(table, 'has_input', 'yes')
    if count < 3:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'запусків із вхідними даними %d, а потрібно щонайменше три' % count)


def rule_pr_2(table, tables, report, rule):
    if not any(num(v) >= 2 for v in table.col('iterations')):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жоден промпт не уточнювався: у журналі немає ітерації')


def rule_pr_3(table, tables, report, rule):
    kinds = set(v.strip() for v in table.col('artifact') if v.strip())
    if len(kinds) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі запуски пішли в один файл: журнал за семестр так не виглядає')


def rule_pr_4(table, tables, report, rule):
    results = [v for v in table.col('result') if v]
    if results and all(v == 'used' for v in results):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жодну відповідь не правили і не відкинули: перевірте, чи це журнал роботи')


def rule_pr_5(table, tables, report, rule):
    dates = set(v for v in table.col('date') if v)
    if len(dates) == 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі запуски датовані одним днем: журнал за семестр зібраний за вечір')


PATH_RE = re.compile(r'/[^,]*\.(csv|md)$', re.IGNORECASE)


def rule_pr_6(table, tables, report, rule):
    count = sum(1 for v in table.col('artifact') if PATH_RE.search((v or '').strip()))
    if count < 3:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'шляхів до файлів у колонці artifact лише %d, а потрібно щонайменше три' % count)


NO_SOURCE_RE = re.compile(r'\.(csv|md|json|txt|xlsx|pdf)\b|N-\d{2}|https?://', re.IGNORECASE)


def rule_er_1(table, tables, report, rule):
    kinds = set(v for v in table.col('error_type') if v)
    if len(kinds) < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі помилки одного типу: перелік не показує меж інструмента')


def rule_er_2(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        value = table.cell(row, 'how_noticed')
        if value and not NO_SOURCE_RE.search(value):
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у помилки %s не названо, з чим звіряли: немає ні файла, ні тега рядка, '
                       'ні посилання' % table.cell(row, 'error_id'))


def rule_er_3(table, tables, report, rule):
    runs = set(v for v in table.col('run_id') if v)
    if len(runs) == 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'усі помилки з одного запуску: це одна невдала спроба, а не межі інструмента')


def rule_ts_1(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        expected = (num(table.cell(row, 'minutes_manual'))
                    - num(table.cell(row, 'minutes_ai'))
                    - num(table.cell(row, 'minutes_review')))
        actual = num(table.cell(row, 'minutes_saved'))
        if abs(expected - actual) > 0.01:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у випадку %s різниця дає %.0f хвилин, а записано %.0f'
                       % (table.cell(row, 'case_id'), expected, actual))


def rule_ts_2(table, tables, report, rule):
    if _count(table, 'basis', 'measured') == 0:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жоден випадок не заміряний: усі числа пригадані')


def rule_ts_3(table, tables, report, rule):
    for idx, row in enumerate(table.rows):
        if num(table.cell(row, 'minutes_review')) <= 0:
            report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                       'у випадку %s на перевірку результату витрачено нуль хвилин'
                       % table.cell(row, 'case_id'))


def rule_ts_4(table, tables, report, rule):
    values = [num(v) for v in table.col('minutes_saved')]
    if values and all(v > 0 for v in values):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'виграш у всіх випадках додатний: жодного разу перевірка не з\'їла економію')


def rule_pl_1(table, tables, report, rule):
    for value, minimum in (('forbidden', 2), ('draft', 2)):
        count = _count(table, 'mode', value)
        if count < minimum:
            report.add(rule['severity'], table.path, 1, rule['id'],
                       'рядків з режимом %s лише %d, а потрібно щонайменше %d'
                       % (value, count, minimum))


FILE_NAME_RE = re.compile(r'\.(csv|md)\b', re.IGNORECASE)


def rule_pl_2(table, tables, report, rule):
    if not any(FILE_NAME_RE.search(v or '') for v in table.col('case')):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жодне правило не називає файла портфеля: політика написана взагалі, '
                   'а не під власний проєкт')


def rule_pl_4(table, tables, report, rule):
    if _count(table, 'mode', 'autonomous') == 0:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'жодного рядка з режимом autonomous: це відмова від інструмента, а не правило роботи з ним')


CONFIDENTIAL_WORDS = ('персональн', 'конфіденц', 'nda', 'приватн')


def rule_pl_3(table, tables, report, rule):
    joined = ' '.join(v.lower() for v in table.col('case'))
    if not any(word in joined for word in CONFIDENTIAL_WORDS):
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'у політиці немає рядка про дані, які не можна віддавати зовнішній моделі')


FILE_RULES = {
    'SRC-1': rule_src_1, 'SRC-2': rule_src_2, 'SRC-3': rule_src_3, 'SRC-4': rule_src_4,
    'AP-1': rule_ap_1, 'AP-2': rule_ap_2, 'AP-3': rule_ap_3, 'AP-4': rule_ap_4, 'AP-5': rule_ap_5,
    'DC-1': rule_dc_1, 'DC-2': rule_dc_2, 'DC-3': rule_dc_3,
    'ST-1': rule_st_1, 'ST-2': rule_st_2, 'ST-3': rule_st_3,
    'SC-1': rule_sc_1, 'SC-2': rule_sc_2, 'SC-3': rule_sc_3,
    'BL-1': rule_bl_1, 'BL-2': rule_bl_2, 'BL-3': rule_bl_3, 'BL-4': rule_bl_4,
    'BL-5': rule_bl_5, 'BL-6': rule_bl_6, 'BL-7': rule_bl_7, 'BL-8': rule_bl_8,
    'BL-9': rule_bl_9, 'BL-10': rule_bl_10, 'BL-11': rule_bl_11,
    'WBS-1': rule_wbs_1, 'WBS-2': rule_wbs_2, 'WBS-3': rule_wbs_3,
    'SCH-1': rule_sch_1, 'SCH-2': rule_sch_2, 'SCH-3': rule_sch_3,
    'SCH-4': rule_sch_4, 'SCH-5': rule_sch_5,
    'RM-1': rule_rm_1, 'RM-3': rule_rm_3,
    'PK-1': rule_pk_1, 'PK-3': rule_pk_3, 'PK-4': rule_pk_4, 'PK-5': rule_pk_5,
    'ES-1': rule_es_1, 'ES-2': rule_es_2, 'ES-3': rule_es_3,
    'ES-4': rule_es_4, 'ES-5': rule_es_5, 'ES-6': rule_es_6,
    'VL-1': rule_vl_1, 'VL-2': rule_vl_2, 'VL-3': rule_vl_3, 'VL-4': rule_vl_4,
    'FC-1': rule_fc_1, 'FC-2': rule_fc_2, 'FC-3': rule_fc_3,
    'RK-1': rule_rk_1, 'RK-2': rule_rk_2, 'RK-3': rule_rk_3,
    'RK-5': rule_rk_5, 'RK-6': rule_rk_6,
    'TD-1': rule_td_1, 'TD-2': rule_td_2,
    'CH-1': rule_ch_1, 'CH-2': rule_ch_2, 'CH-3': rule_ch_3,
    'RC-1': rule_rc_1, 'RC-2': rule_rc_2, 'RC-3': rule_rc_3, 'RC-4': rule_rc_4, 'RC-5': rule_rc_5,
    'RC-6': rule_rc_6, 'RC-7': rule_rc_7, 'RC-8': rule_rc_8, 'RC-9': rule_rc_9,
    'CM-1': rule_cm_1, 'CM-2': rule_cm_2, 'CM-3': rule_cm_3,
    'FL-1': rule_fl_1, 'FL-2': rule_fl_2, 'FL-3': rule_fl_3,
    'FL-4': rule_fl_4, 'FL-5': rule_fl_5,
    'FD-1': rule_fd_1, 'FD-2': rule_fd_2, 'FD-3': rule_fd_3,
    'DB-1': rule_db_1, 'DB-2': rule_db_2, 'DB-3': rule_db_3,
    'DB-4': rule_db_4, 'DB-5': rule_db_5,
    'IM-1': rule_im_1, 'IM-2': rule_im_2, 'IM-3': rule_im_3, 'IM-4': rule_im_4,
    'BG-1': rule_bg_1, 'BG-2': rule_bg_2, 'BG-3': rule_bg_3, 'BG-4': rule_bg_4,
    'BG-5': rule_bg_5, 'BG-6': rule_bg_6, 'BG-7': rule_bg_7,
    'RT-1': rule_rt_1, 'RT-2': rule_rt_2, 'RT-3': rule_rt_3,
    'PF-1': rule_pf_1, 'PF-2': rule_pf_2, 'PF-3': rule_pf_3, 'PF-4': rule_pf_4,
    'TK-1': rule_tk_1, 'TK-2': rule_tk_2, 'TK-3': rule_tk_3, 'TK-4': rule_tk_4, 'TK-5': rule_tk_5,
    'PR-1': rule_pr_1, 'PR-2': rule_pr_2, 'PR-3': rule_pr_3, 'PR-4': rule_pr_4,
    'PR-5': rule_pr_5, 'PR-6': rule_pr_6,
    'ER-1': rule_er_1, 'ER-2': rule_er_2, 'ER-3': rule_er_3,
    'TS-1': rule_ts_1, 'TS-2': rule_ts_2, 'TS-3': rule_ts_3, 'TS-4': rule_ts_4,
    'PL-1': rule_pl_1, 'PL-2': rule_pl_2, 'PL-3': rule_pl_3, 'PL-4': rule_pl_4,
}

# Правила, які покриті перевіркою посилань або іншим правилом.
COVERED_BY_REFS = {'PK-2'}


# ------------------------------------------------------------ наскрізні правила

def filled(table):
    """Таблиця існує і має рядки. Порожній файл із самим заголовком означає
    «роботу ще не починали», і наскрізні правила на нього не спрацьовують:
    інакше здача ЛР6 ламалась би об порожній roadmap.csv із шаблону."""
    return table is not None and bool(table.rows)


def read_text(root, rel_path):
    full = os.path.join(root, rel_path)
    if not os.path.isfile(full):
        return None
    with open(full, encoding='utf-8-sig') as fh:
        return fh.read()


def cross_x4(root, tables, report, rule):
    budget = tables.get('lr16_budget/budget.csv')
    wbs = tables.get('lr07_wbs/wbs.csv')
    if not filled(budget) or not filled(wbs):
        return
    labor = sum(num(budget.cell(row, 'hours')) for row in budget.rows
                if budget.cell(row, 'category') == 'labor')
    parents = set(v for v in wbs.col('parent_id') if v)
    leaves = sum(num(wbs.cell(row, 'estimate_hours')) for row in wbs.rows
                 if wbs.cell(row, 'wbs_id') not in parents)
    if leaves and abs(labor - leaves) > leaves * 0.45:
        report.add(rule['severity'], 'lr16_budget/budget.csv', 1, rule['id'],
                   'годин labor у кошторисі %s, а в листах WBS %s: розбіжність більша за 45 відсотків, '
                   'тобто числа відрізняються не на відсотки, а в рази'
                   % (labor, leaves))


def cross_x5(root, tables, report, rule):
    backlog = tables.get('lr06_backlog/backlog.csv')
    estimates = tables.get('lr08_poker/estimates.csv')
    if not filled(backlog) or not filled(estimates):
        return
    estimated = set(v for v in estimates.col('story_id') if v)
    for idx, row in enumerate(backlog.rows):
        if backlog.cell(row, 'release') != 'REL-1':
            continue
        story = backlog.cell(row, 'story_id')
        if story not in estimated:
            report.add(rule['severity'], 'lr06_backlog/backlog.csv', backlog.line(idx), rule['id'],
                       'історія %s із першого релізу не оцінена в estimates.csv' % story)


def cross_x6(root, tables, report, rule, spec_version=''):
    text = read_text(root, 'README.md')
    if text is None:
        return
    found = VERSION_RE.findall(text)
    if not found:
        report.add(rule['severity'], 'README.md', 1, rule['id'],
                   'у картці студента немає версії схеми артефактів, очікується %s' % spec_version)
    elif spec_version not in found:
        report.add(rule['severity'], 'README.md', 1, rule['id'],
                   'у картці студента версія схеми %s, а спека курсу має версію %s'
                   % (', '.join(found), spec_version))


def cross_x7(root, tables, report, rule):
    sources = tables.get('lr01_case/sources.csv')
    text = read_text(root, 'lr01_case/README.md')
    if sources is None or text is None:
        return
    used = set(SOURCE_REF_RE.findall(text))
    for idx, row in enumerate(sources.rows):
        sid = sources.cell(row, 'source_id')
        if sid and sid not in used:
            report.add(rule['severity'], 'lr01_case/sources.csv', sources.line(idx), rule['id'],
                       'джерело %s не згадується в тексті розтину як [%s]' % (sid, sid))


def cross_x8(root, tables, report, rule):
    sources = tables.get('lr01_case/sources.csv')
    text = read_text(root, 'lr01_case/README.md')
    if sources is None or text is None:
        return
    known = set(v for v in sources.col('source_id') if v)
    for sid in sorted(set(SOURCE_REF_RE.findall(text))):
        if sid not in known:
            report.add(rule['severity'], 'lr01_case/README.md', 1, rule['id'],
                       'у тексті є посилання [%s], якого немає в sources.csv' % sid)


def _lr02_tables(tables):
    return tables.get('lr02_approach/approach.csv'), tables.get('lr02_approach/decision.csv')


def cross_x9(root, tables, report, rule):
    approach, decision = _lr02_tables(tables)
    if approach is None or decision is None:
        return
    by_case = {}
    for row in approach.rows:
        case = approach.cell(row, 'case_id')
        if case:
            by_case.setdefault(case, set()).add(approach.cell(row, 'criterion'))
    for idx, row in enumerate(decision.rows):
        case = decision.cell(row, 'case_id')
        if not case:
            continue
        missing = [c for c in CRITERIA if c not in by_case.get(case, set())]
        if missing:
            report.add(rule['severity'], 'lr02_approach/decision.csv', decision.line(idx), rule['id'],
                       'рішення по кейсу %s є, а в матриці не оцінені критерії: %s'
                       % (case, ', '.join(missing)))


def cross_x10(root, tables, report, rule):
    approach, decision = _lr02_tables(tables)
    if approach is None or decision is None:
        return
    pulls = {}
    for row in approach.rows:
        pulls[(approach.cell(row, 'case_id'), approach.cell(row, 'criterion'))] = approach.cell(row, 'pull')
    for idx, row in enumerate(decision.rows):
        case = decision.cell(row, 'case_id')
        chosen = decision.cell(row, 'chosen_approach')
        conflict = decision.cell(row, 'main_conflict')
        if not (case and chosen and conflict):
            continue
        pull = pulls.get((case, conflict))
        if pull is None:
            continue
        if chosen == 'hybrid':
            if pull == 'neutral':
                report.add(rule['severity'], 'lr02_approach/decision.csv', decision.line(idx), rule['id'],
                           'кейс %s: критерій `%s` у матриці нейтральний, тому конфліктом він бути не може'
                           % (case, conflict))
            continue
        want = OPPOSITE.get(chosen)
        if want and pull != want:
            report.add(rule['severity'], 'lr02_approach/decision.csv', decision.line(idx), rule['id'],
                       'кейс %s: обрано %s, а критерій `%s` у матриці має pull `%s`. '
                       'Конфліктом є критерій, який тягне в бік `%s`'
                       % (case, chosen, conflict, pull, want))


def cross_x11(root, tables, report, rule):
    schedule = tables.get('lr07_wbs/schedule.csv')
    wbs = tables.get('lr07_wbs/wbs.csv')
    if not filled(schedule) or not filled(wbs):
        return
    parents = set(v for v in wbs.col('parent_id') if v)
    for idx, row in enumerate(schedule.rows):
        wbs_id = schedule.cell(row, 'wbs_id')
        if wbs_id and wbs_id in parents:
            report.add(rule['severity'], 'lr07_wbs/schedule.csv', schedule.line(idx), rule['id'],
                       'робота %s посилається на вузол %s, у якого є діти: плануйте листові пакети робіт'
                       % (schedule.cell(row, 'task_id'), wbs_id))


def cross_x12(root, tables, report, rule):
    schedule = tables.get('lr07_wbs/schedule.csv')
    wbs = tables.get('lr07_wbs/wbs.csv')
    if schedule is None or wbs is None:
        return
    parents = set(v for v in wbs.col('parent_id') if v)
    planned = set(v for v in schedule.col('wbs_id') if v)
    for idx, row in enumerate(wbs.rows):
        wbs_id = wbs.cell(row, 'wbs_id')
        if not wbs_id or wbs_id in parents:
            continue
        if wbs_id not in planned:
            report.add(rule['severity'], 'lr07_wbs/wbs.csv', wbs.line(idx), rule['id'],
                       'пакет %s не має жодної роботи в календарному плані' % wbs_id)


def cross_x13(root, tables, report, rule):
    backlog = tables.get('lr06_backlog/backlog.csv')
    roadmap = tables.get('lr07_wbs/roadmap.csv')
    if not filled(backlog) or not filled(roadmap):
        return
    known = set(v for v in roadmap.col('release_id') if v)
    for idx, row in enumerate(backlog.rows):
        release = backlog.cell(row, 'release')
        if release and release not in known:
            report.add(rule['severity'], 'lr06_backlog/backlog.csv', backlog.line(idx), rule['id'],
                       'історія %s запланована в реліз %s, якого немає в roadmap.csv'
                       % (backlog.cell(row, 'story_id'), release))


def cross_x14(root, tables, report, rule):
    backlog = tables.get('lr06_backlog/backlog.csv')
    criteria = tables.get('lr05_charter/success_criteria.csv')
    if not filled(backlog) or not filled(criteria):
        return
    served = set(backlog.cell(row, 'success_criterion') for row in backlog.rows
                 if backlog.cell(row, 'release') == 'REL-1')
    for idx, row in enumerate(criteria.rows):
        key = criteria.cell(row, 'criterion_id')
        if key and key not in served:
            report.add(rule['severity'], 'lr05_charter/success_criteria.csv', criteria.line(idx), rule['id'],
                       'критерій %s не має жодної історії першого релізу: '
                       'його нічим буде виконати до релізу' % key)


def cross_x15(root, tables, report, rule):
    risks = tables.get('lr11_risks_quality/risks.csv')
    velocity = tables.get('lr09_forecast/velocity.csv')
    if not filled(risks) or not filled(velocity):
        return
    ends = [parse_date(velocity.cell(row, 'end_date')) for row in velocity.rows]
    ends = [d for d in ends if d]
    if not ends:
        return
    first_end = min(ends)
    for idx, row in enumerate(risks.rows):
        seen = parse_date(risks.cell(row, 'review_date'))
        if seen and seen < first_end:
            report.add(rule['severity'], 'lr11_risks_quality/risks.csv', risks.line(idx), rule['id'],
                       'ризик %s переглянутий %s, а перший спринт закрився %s: '
                       'це перша оцінка, а не перегляд'
                       % (risks.cell(row, 'risk_id'), seen, first_end))


def _source_file_body(root, source):
    """Рядки файла-джерела без рядка заголовків CSV. None, якщо файла немає."""
    full = os.path.join(root, source)
    if not os.path.isfile(full):
        return None
    with open(full, encoding='utf-8-sig') as fh:
        lines = [line for line in fh.read().splitlines() if line.strip()]
    return lines[1:] if source.endswith('.csv') else lines


def cross_x16(root, tables, report, rule):
    # Той самий контракт для двох файлів: дашборд ЛР14 і таблиця впливу ЛР15.
    targets = (('lr14_metrics/dashboard.csv', 'metric_id', 'метрика'),
               ('lr15_status_report/impact.csv', 'dimension', 'вимір'))
    for path, key_col, noun in targets:
        table = tables.get(path)
        if not filled(table):
            continue
        for idx, row in enumerate(table.rows):
            source = table.cell(row, 'source_file').strip()
            if not source:
                continue
            body = _source_file_body(root, source)
            if body is None:
                report.add(rule['severity'], path, table.line(idx), rule['id'],
                           '%s %s посилається на `%s`, а такого файла в портфелі немає'
                           % (noun, table.cell(row, key_col), source))
                continue
            if not body:
                report.add(rule['severity'], path, table.line(idx), rule['id'],
                           '%s %s береться з файла `%s`, у якому немає рядків: '
                           'це заготовка з шаблону, а не джерело числа'
                           % (noun, table.cell(row, key_col), source))


def _lr15_started(tables):
    """ЛР15 вважається початою, коли заповнена її власна таблиця впливу."""
    return filled(tables.get('lr15_status_report/impact.csv'))


def _read_text(root, rel_path):
    full = os.path.join(root, rel_path)
    if not os.path.isfile(full):
        return None
    with open(full, encoding='utf-8-sig') as fh:
        return fh.read()


def cross_x17(root, tables, report, rule):
    if not _lr15_started(tables):
        return
    dashboard = tables.get('lr14_metrics/dashboard.csv')
    if not filled(dashboard):
        return
    path = 'lr15_status_report/README.md'
    text = _read_text(root, path)
    if text is None:
        report.add(rule['severity'], path, 1, rule['id'],
                   'файла статус-звіту немає, а таблиця впливу вже заповнена')
        return
    known = set(v for v in dashboard.col('metric_id') if v)
    named = []
    for key in re.findall(r'MT-\d{2}', text):
        if key not in named:
            named.append(key)
    missing = [k for k in named if k not in known]
    for key in missing:
        report.add(rule['severity'], path, 1, rule['id'],
                   'звіт посилається на метрику %s, якої немає в lr14_metrics/dashboard.csv' % key)
    if len(named) < 3:
        report.add(rule['severity'], path, 1, rule['id'],
                   'у звіті названо метрик дашборда: %d, а потрібно щонайменше три' % len(named))


DECISION_WORDS = (('approve', 'accepted'), ('reject', 'rejected'), ('defer', 'deferred'))


def _change_request_decision(text):
    """Рішення із change_request.md. None, якщо його не видно однозначно."""
    section = text
    head = re.search(r'^##\s*4\.', text, re.M)
    if head:
        tail = re.search(r'^##\s', text[head.end():], re.M)
        section = text[head.end():head.end() + tail.start()] if tail else text[head.end():]
    line = None
    for candidate in section.splitlines():
        if re.match(r'^\s*\|\s*Рішення\s*\|', candidate):
            line = candidate
            break
    scope = line if line is not None else section
    found = set()
    for word, value in DECISION_WORDS:
        if re.search(word, scope, re.I):
            found.add(value)
    if len(found) == 1:
        return found.pop()
    return None


def cross_x18(root, tables, report, rule):
    if not _lr15_started(tables):
        return
    path = 'lr15_status_report/change_request.md'
    text = _read_text(root, path)
    if text is None:
        report.add(rule['severity'], path, 1, rule['id'],
                   'файла запиту на зміну немає, а таблиця впливу вже заповнена')
        return
    decision = _change_request_decision(text)
    if decision is None:
        report.add(rule['severity'], path, 1, rule['id'],
                   'у розділі 4 не видно одного рішення: у рядку `Рішення` має лишитись '
                   'рівно одне слово з approve, reject або defer')
        return
    changelog = tables.get('lr11_risks_quality/changelog.csv')
    if not filled(changelog):
        return
    rows = [row for row in changelog.rows if changelog.cell(row, 'source') == 'course_event']
    if not rows:
        return
    rows.sort(key=lambda row: changelog.cell(row, 'date'))
    logged = changelog.cell(rows[-1], 'decision')
    if logged and logged != decision:
        report.add(rule['severity'], path, 1, rule['id'],
                   'у запиті на зміну рішення `%s`, а в журналі змін за подією курсу `%s`: '
                   'одна подія не може мати двох рішень на ту саму дату'
                   % (decision, logged))


def cross_x19(root, tables, report, rule):
    budget = tables.get('lr16_budget/budget.csv')
    card = tables.get('lr16_budget/rate_card.csv')
    if not filled(budget) or not filled(card):
        return
    known = set()
    for row in card.rows:
        value = card.cell(row, 'rate')
        if NUMBER_RE.match(value):
            known.add(round(float(value), 2))
    for idx, row in enumerate(budget.rows):
        if budget.cell(row, 'category') != 'labor':
            continue
        value = budget.cell(row, 'rate')
        if not NUMBER_RE.match(value):
            continue
        if round(float(value), 2) not in known:
            report.add(rule['severity'], 'lr16_budget/budget.csv', budget.line(idx), rule['id'],
                       'ставки %s немає в rate_card.csv: кошторис рахується за прайсом, '
                       'а не поруч із ним' % value)


def cross_x20(root, tables, report, rule):
    budget = tables.get('lr16_budget/budget.csv')
    card = tables.get('lr16_budget/rate_card.csv')
    if not filled(budget) or not filled(card):
        return
    labor = sum(num(budget.cell(row, 'hours')) for row in budget.rows
                if budget.cell(row, 'category') == 'labor')
    planned = sum(num(card.cell(row, 'hours')) for row in card.rows)
    if planned and abs(labor - planned) > planned * 0.15:
        report.add(rule['severity'], 'lr16_budget/budget.csv', 1, rule['id'],
                   'годин labor у кошторисі %s, а в rate card %s: розбіжність більша '
                   'за 15 відсотків' % (labor, planned))


def cross_x21(root, tables, report, rule):
    fact = tables.get('lr16_budget/plan_fact.csv')
    velocity = tables.get('lr09_forecast/velocity.csv')
    if not filled(fact) or not filled(velocity):
        return
    done = {}
    for row in velocity.rows:
        sprint = velocity.cell(row, 'sprint')
        if sprint:
            done[sprint] = num(velocity.cell(row, 'points_done'))
    for idx, row in enumerate(fact.rows):
        sprint = fact.cell(row, 'sprint')
        if sprint not in done:
            continue
        if abs(num(fact.cell(row, 'actual_points')) - done[sprint]) > 0.01:
            report.add(rule['severity'], 'lr16_budget/plan_fact.csv', fact.line(idx), rule['id'],
                       'за спринт %s у velocity.csv закрито %s points, а тут стоїть %s'
                       % (sprint, done[sprint], fact.cell(row, 'actual_points')))


NOTE_TAG_RE = re.compile(r'\bN-\d{2}\b')


def cross_x22(root, tables, report, rule):
    tasks = tables.get('lr17_ai_assistant/tasks.csv')
    if not filled(tasks):
        return
    text = read_text(root, 'lr17_ai_assistant/input_notes.md')
    if text is None:
        report.add(rule['severity'], 'lr17_ai_assistant/tasks.csv', 1, rule['id'],
                   'немає файла lr17_ai_assistant/input_notes.md: перевірити теги рядків нічим')
        return
    tags = set(NOTE_TAG_RE.findall(text))
    if not tags:
        report.add(rule['severity'], 'lr17_ai_assistant/input_notes.md', 1, rule['id'],
                   'у нотатках немає жодного тега рядка формату N-NN: '
                   'копія зроблена без тегів')
        return
    for idx, row in enumerate(tasks.rows):
        value = tasks.cell(row, 'source_line')
        if value and value not in tags:
            report.add(rule['severity'], 'lr17_ai_assistant/tasks.csv', tasks.line(idx), rule['id'],
                       'рядка %s у ваших нотатках немає' % value)


def cross_x23(root, tables, report, rule):
    prompts = tables.get('lr17_ai_assistant/prompts.csv')
    if not filled(prompts):
        return
    for idx, row in enumerate(prompts.rows):
        value = (prompts.cell(row, 'artifact') or '').strip()
        if not value:
            continue
        if not PATH_RE.search(value):
            report.add(rule['severity'], 'lr17_ai_assistant/prompts.csv', prompts.line(idx), rule['id'],
                       'значення «%s» не схоже на шлях до файла портфеля' % value)
            continue
        if not os.path.exists(os.path.join(root, value)):
            report.add(rule['severity'], 'lr17_ai_assistant/prompts.csv', prompts.line(idx), rule['id'],
                       'файла %s у репозиторії немає' % value)


def cross_x24(root, tables, report, rule):
    prompts = tables.get('lr17_ai_assistant/prompts.csv')
    if not filled(prompts):
        return
    outside = sum(1 for row in prompts.rows
                  if not (prompts.cell(row, 'artifact') or '').strip().startswith('lr17_ai_assistant'))
    if outside < 3:
        report.add(rule['severity'], 'lr17_ai_assistant/prompts.csv', 1, rule['id'],
                   'поза папкою ЛР17 лише %d запусків: журнал за семестр зібраний з однієї роботи'
                   % outside)


CROSS_RULES = {'X-4': cross_x4, 'X-5': cross_x5, 'X-6': cross_x6, 'X-7': cross_x7, 'X-8': cross_x8,
               'X-9': cross_x9, 'X-10': cross_x10,
               'X-11': cross_x11, 'X-12': cross_x12,
               'X-13': cross_x13, 'X-14': cross_x14, 'X-15': cross_x15,
               'X-16': cross_x16, 'X-17': cross_x17, 'X-18': cross_x18,
               'X-19': cross_x19, 'X-20': cross_x20, 'X-21': cross_x21,
               'X-22': cross_x22, 'X-23': cross_x23, 'X-24': cross_x24}
# X-1 і X-2 покриті перевіркою посилань колонок, X-3 порахований правилом FC-2.
CROSS_COVERED = {'X-1', 'X-2', 'X-3'}

CROSS_SCOPE = {
    'X-4': ('lr16_budget', 'lr07_wbs'),
    'X-5': ('lr06_backlog', 'lr08_poker'),
    'X-6': ('README.md',),
    'X-7': ('lr01_case',),
    'X-8': ('lr01_case',),
    'X-9': ('lr02_approach',),
    'X-10': ('lr02_approach',),
    'X-11': ('lr07_wbs',),
    'X-12': ('lr07_wbs',),
    'X-13': ('lr06_backlog', 'lr07_wbs'),
    'X-14': ('lr05_charter', 'lr06_backlog'),
    'X-15': ('lr11_risks_quality', 'lr09_forecast'),
    'X-16': ('lr14_metrics', 'lr15_status_report'),
    'X-17': ('lr14_metrics', 'lr15_status_report'),
    'X-18': ('lr11_risks_quality', 'lr15_status_report'),
    'X-19': ('lr16_budget',),
    'X-20': ('lr16_budget',),
    'X-21': ('lr16_budget', 'lr09_forecast'),
    'X-22': ('lr17_ai_assistant',),
    'X-23': ('lr17_ai_assistant',),
    'X-24': ('lr17_ai_assistant',),
}


# ------------------------------------------------------------------------ запуск

def in_scope(rel_path, scope):
    if scope is None:
        return True
    return rel_path == scope or rel_path.startswith(scope.rstrip('/') + '/')


def main():
    spec = load_spec()
    args = [a for a in sys.argv[1:] if not a.startswith('-')]
    scope = None
    if args and args[0] not in ('.', './'):
        scope = args[0].rstrip('/')

    root = ROOT
    report = Report()
    tables = {}

    for spec_file in spec['files']:
        full = os.path.join(root, spec_file['path'])
        if not os.path.isfile(full):
            continue
        table, notes = read_table(root, spec_file['path'])
        tables[spec_file['path']] = table
        for level, line, message in notes:
            report.add(level, spec_file['path'], line, 'csv', message)

    checked = 0
    for spec_file in spec['files']:
        path = spec_file['path']
        table = tables.get(path)
        if table is None or not in_scope(path, scope):
            continue
        checked += 1
        if not check_header(spec_file, table, report):
            continue
        if not table.rows:
            # Порожній файл із одним рядком заголовків означає «роботу ще не
            # починали». Це попередження, а не помилка: шаблон репозиторію має
            # проходити перевірку чисто.
            report.add(WARNING, path, 1, 'empty',
                       'файл поки порожній, у ньому тільки рядок заголовків')
            continue
        check_mechanics(spec_file, table, spec['enums'], report)
        check_refs(spec_file, table, tables, report)
        for rule in spec_file.get('rules', []):
            if rule['id'] in COVERED_BY_REFS:
                continue
            handler = FILE_RULES.get(rule['id'])
            if handler is None:
                report.add(SKIPPED, path, 1, rule['id'],
                           'формальній перевірці не піддається, читає викладач: %s' % rule['text'])
                continue
            handler(table, tables, report, rule)

    for rule in spec['cross_file_rules']:
        if rule['id'] in CROSS_COVERED:
            continue
        handler = CROSS_RULES.get(rule['id'])
        if handler is None:
            continue
        if scope is not None and not any(in_scope(p, scope) or p == scope
                                         for p in CROSS_SCOPE.get(rule['id'], ())):
            continue
        if rule['id'] == 'X-6':
            handler(root, tables, report, rule, spec['schema_version'])
        else:
            handler(root, tables, report, rule)

    return output(report, spec, checked, scope)


def output(report, spec, checked, scope):
    order = {ERROR: 0, WARNING: 1, SKIPPED: 2}
    items = sorted(report.items, key=lambda i: (order[i[0]], i[1], i[2]))
    print('Валідатор курсу «Управління ІТ проєктами», спека %s.' % spec['schema_version'])
    print('Перевірено файлів: %d%s.' % (checked, '' if scope is None else ', область: %s' % scope))
    print('')
    if not items:
        print('Зауважень немає.')
    for level, path, line, rule, message in items:
        print('%-12s %-34s рядок %-4s %-8s %s' % (LEVEL_NAME[level], path, line, rule, message))
    print('')
    print('Помилок: %d. Попереджень: %d. Пропущено правил: %d.'
          % (len(report.errors()), len(report.warnings()), len(report.skipped())))
    if report.errors():
        print('Помилки рівня «помилка» блокують здачу: виправте їх до дедлайну.')
        return 1
    print('Механіка пройдена. Зміст роботи оцінюється окремо за рубрикою.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
