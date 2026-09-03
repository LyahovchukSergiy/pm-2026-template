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


def rule_rm_2(table, tables, report, rule):
    seen = {}
    for idx, row in enumerate(table.rows):
        for story in [p.strip() for p in table.cell(row, 'story_ids').split(';') if p.strip()]:
            if story in seen:
                report.add(rule['severity'], table.path, table.line(idx), rule['id'],
                           'історія `%s` уже стоїть у релізі рядка %d' % (story, seen[story]))
            else:
                seen[story] = table.line(idx)


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
        count = sum(1 for _, row in rows if table.cell(row, 'role') == 'A')
        if count != 1:
            report.add(rule['severity'], table.path, table.line(rows[0][0]), rule['id'],
                       'активність %s має %d ролей A, а має бути рівно одна' % (activity, count))


def rule_rc_3(table, tables, report, rule):
    for activity, rows in sorted(_by_activity(table).items()):
        if not any(table.cell(row, 'role') == 'R' for _, row in rows):
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
    if len(table.rows) < 12:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'карток %d, для перцентиля потрібно щонайменше дванадцять' % len(table.rows))


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
    if count > 1:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків категорії management_reserve %d, а дозволено не більше одного' % count)


def rule_bg_5(table, tables, report, rule):
    count = sum(1 for v in table.col('category') if v == 'labor')
    if count < 2:
        report.add(rule['severity'], table.path, 1, rule['id'],
                   'рядків категорії labor %d: кошторис однієї ролі це не кошторис команди' % count)


FILE_RULES = {
    'SRC-1': rule_src_1, 'SRC-2': rule_src_2, 'SRC-3': rule_src_3, 'SRC-4': rule_src_4,
    'AP-1': rule_ap_1, 'AP-2': rule_ap_2, 'AP-3': rule_ap_3, 'AP-4': rule_ap_4, 'AP-5': rule_ap_5,
    'DC-1': rule_dc_1, 'DC-2': rule_dc_2, 'DC-3': rule_dc_3,
    'ST-1': rule_st_1, 'ST-2': rule_st_2,
    'BL-1': rule_bl_1, 'BL-2': rule_bl_2,
    'WBS-1': rule_wbs_1, 'WBS-2': rule_wbs_2, 'WBS-3': rule_wbs_3,
    'RM-2': rule_rm_2, 'RM-3': rule_rm_3,
    'PK-1': rule_pk_1, 'PK-3': rule_pk_3,
    'ES-1': rule_es_1, 'ES-2': rule_es_2, 'ES-3': rule_es_3,
    'VL-1': rule_vl_1, 'VL-2': rule_vl_2,
    'FC-1': rule_fc_1, 'FC-2': rule_fc_2,
    'RK-1': rule_rk_1, 'RK-2': rule_rk_2, 'RK-3': rule_rk_3,
    'TD-1': rule_td_1,
    'RC-1': rule_rc_1, 'RC-2': rule_rc_2, 'RC-3': rule_rc_3, 'RC-4': rule_rc_4, 'RC-5': rule_rc_5,
    'CM-1': rule_cm_1,
    'FL-1': rule_fl_1, 'FL-2': rule_fl_2, 'FL-3': rule_fl_3,
    'BG-1': rule_bg_1, 'BG-2': rule_bg_2, 'BG-3': rule_bg_3, 'BG-5': rule_bg_5,
}

# Правила, які покриті перевіркою посилань або іншим правилом.
COVERED_BY_REFS = {'RM-1', 'PK-2'}


# ------------------------------------------------------------ наскрізні правила

def read_text(root, rel_path):
    full = os.path.join(root, rel_path)
    if not os.path.isfile(full):
        return None
    with open(full, encoding='utf-8-sig') as fh:
        return fh.read()


def cross_x4(root, tables, report, rule):
    budget = tables.get('lr16_budget/budget.csv')
    wbs = tables.get('lr07_wbs/wbs.csv')
    if budget is None or wbs is None:
        return
    labor = sum(num(budget.cell(row, 'hours')) for row in budget.rows
                if budget.cell(row, 'category') == 'labor')
    parents = set(v for v in wbs.col('parent_id') if v)
    leaves = sum(num(wbs.cell(row, 'estimate_hours')) for row in wbs.rows
                 if wbs.cell(row, 'wbs_id') not in parents)
    if leaves and abs(labor - leaves) > leaves * 0.15:
        report.add(rule['severity'], 'lr16_budget/budget.csv', 1, rule['id'],
                   'годин labor у кошторисі %s, а в листах WBS %s: розбіжність більша за 15 відсотків'
                   % (labor, leaves))


def cross_x5(root, tables, report, rule):
    roadmap = tables.get('lr07_wbs/roadmap.csv')
    estimates = tables.get('lr08_poker/estimates.csv')
    if roadmap is None or estimates is None:
        return
    estimated = set(v for v in estimates.col('story_id') if v)
    for idx, row in enumerate(roadmap.rows):
        if roadmap.cell(row, 'release_id') != 'REL-1':
            continue
        for story in [p.strip() for p in roadmap.cell(row, 'story_ids').split(';') if p.strip()]:
            if story not in estimated:
                report.add(rule['severity'], 'lr07_wbs/roadmap.csv', roadmap.line(idx), rule['id'],
                           'історія %s із першого релізу не оцінена в estimates.csv' % story)


def cross_x6(root, tables, report, rule, spec_version=''):
    text = read_text(root, 'README.md')
    if text is None:
        return
    found = VERSION_RE.findall(text)
    if not found:
        report.add(rule['severity'], 'README.md', 1, rule['id'],
                   'у картці команди немає версії схеми артефактів, очікується %s' % spec_version)
    elif spec_version not in found:
        report.add(rule['severity'], 'README.md', 1, rule['id'],
                   'у картці команди версія схеми %s, а спека курсу має версію %s'
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


CROSS_RULES = {'X-4': cross_x4, 'X-5': cross_x5, 'X-6': cross_x6, 'X-7': cross_x7, 'X-8': cross_x8,
               'X-9': cross_x9, 'X-10': cross_x10}
# X-1 і X-2 покриті перевіркою посилань колонок, X-3 порахований правилом FC-2.
CROSS_COVERED = {'X-1', 'X-2', 'X-3'}

CROSS_SCOPE = {
    'X-4': ('lr16_budget', 'lr07_wbs'),
    'X-5': ('lr07_wbs', 'lr08_poker'),
    'X-6': ('README.md',),
    'X-7': ('lr01_case',),
    'X-8': ('lr01_case',),
    'X-9': ('lr02_approach',),
    'X-10': ('lr02_approach',),
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
