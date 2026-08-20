# -*- coding: utf-8 -*-
import sys
import re

with open(r'C:\Users\darsh\Github\Empyrean-V2-Backend\docs\backlogs.md', 'r', encoding='utf-8') as f:
    content = f.read()

# Find section boundaries
medium_start = content.find('## 🟡 MEDIUM Priority Problems')
low_start = content.find('## 🔵 LOW Priority Issues')

if medium_start == -1:
    medium_start = content.find('## MEDIUM Priority Problems')
if low_start == -1:
    low_start = content.find('## LOW Priority Issues')

medium_section = content[medium_start:low_start] if medium_start != -1 and low_start != -1 else ''
low_section = content[low_start:] if low_start != -1 else ''

# Find MEDIUM issues
medium_issues = []
for match in re.finditer(r'###\s+(\d+)\s+-\s*(.+?)(?=\n- \*\*File\*|---|\Z)', medium_section, re.DOTALL):
    issue_num = match.group(1)
    title = match.group(2).strip()
    # Find if it has a status line
    issue_content = medium_section[match.start():match.end()+200]
    status_match = re.search(r'- \*\*Status\*\*:\s*(\S+)', issue_content)
    status = status_match.group(1) if status_match else 'Not marked'

    if 'RESOLVED' not in status and 'FIXED' not in status:
        medium_issues.append({
            'num': issue_num,
            'title': title,
            'status': status
        })

# Find LOW issues
low_issues = []
for match in re.finditer(r'###\s+(\d+)\s+-\s*(.+?)(?=\n- \*\*File\*|---|\Z)', low_section, re.DOTALL):
    issue_num = match.group(1)
    title = match.group(2).strip()
    issue_content = low_section[match.start():match.end()+200]
    status_match = re.search(r'- \*\*Status\*\*:\s*(\S+)', issue_content)
    status = status_match.group(1) if status_match else 'Not marked'

    if 'RESOLVED' not in status and 'FIXED' not in status:
        low_issues.append({
            'num': issue_num,
            'title': title,
            'status': status
        })

print('=' * 80)
print(f'MEDIUM priority issues pending: {len(medium_issues)}')
print('=' * 80)
for issue in medium_issues:
    print(f'\nM-{issue["num"]}: {issue["title"][:80]}')
    print(f'  Status: {issue["status"]}')

print('\n' + '=' * 80)
print(f'LOW priority issues pending: {len(low_issues)}')
print('=' * 80)
for issue in low_issues:
    print(f'\nL-{issue["num"]}: {issue["title"][:80]}')
    print(f'  Status: {issue["status"]}')

print('\n' + '=' * 80)
print(f'TOTAL UNRESOLVED PENDING: {len(medium_issues) + len(low_issues)}')
print('=' * 80)

# Return quick summary
print(f'\nSummary:')
print(f'  MEDIUM: {len(medium_issues)} pending')
print(f'  LOW: {len(low_issues)} pending')
print(f'  Total: {len(medium_issues) + len(low_issues)}')