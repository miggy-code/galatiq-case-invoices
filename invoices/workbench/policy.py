"""Workbench constants: demo roles, routing teams and human-readable reason codes.

Nothing here weakens the core policy. The core still decides eligibility; this
module only describes who may do what in the local demo and how reasons read.
"""
from collections import namedtuple

POLICY_VERSION = 'main-v1-working+workbench-1'

Actor = namedtuple('Actor', 'id name role')

# Local demo identities. A cookie selects one; there is no authentication, so
# this is NOT access control. Server-side checks still use the role table so
# the workflow rules are exercised the way real RBAC would exercise them.
ACTORS = {
    'clerk': Actor('clerk', 'Demo AP Clerk', 'ap_clerk'),
    'manager': Actor('manager', 'Demo AP Manager', 'ap_manager'),
    'vp': Actor('vp', 'Demo VP Finance', 'vp_finance'),
    'observer': Actor('observer', 'Demo Observer', 'observer'),
}
SYSTEM = Actor('system', 'System', 'system')
DEFAULT_ACTOR = ACTORS['observer']

ROLE_LABELS = {
    'ap_clerk': 'AP Clerk',
    'ap_manager': 'AP Manager',
    'vp_finance': 'VP Finance',
    'observer': 'Observer (read-only)',
    'system': 'System',
}

_WORK = {'edit', 'select_version', 'assign', 'request_correction', 'escalate',
         'comment', 'revalidate'}
PERMISSIONS = {
    'ap_clerk': _WORK,
    'ap_manager': _WORK | {'approve_ap', 'reject', 'reopen', 'pay'},
    # High-value sign-off belongs to the VP; the VP does not substitute for AP.
    'vp_finance': _WORK | {'approve_vp', 'reject', 'reopen', 'pay'},
    'observer': set(),
}

# Routing is a prototype table, not an organisation chart. Real employee
# names, a vendor registry and FX rates are deliberately not invented.
TEAMS = ['AP Operations', 'Procurement', 'Inventory / Warehouse',
         'Vendor Management', 'Treasury', 'AP Manager', 'VP Finance']
CORRECTION_PARTIES = ['Vendor', 'Requester / buyer', 'Internal data entry']

# code: (label, default team). Order doubles as routing priority.
REASONS = {
    'parse_error': ('Unreadable document', 'AP Operations'),
    'revision_conflict': ('Competing versions', 'AP Operations'),
    'missing_vendor': ('Vendor missing', 'AP Operations'),
    'missing_invoice_number': ('Invoice number missing', 'AP Operations'),
    'missing_date': ('Invoice date missing', 'AP Operations'),
    'missing_due_date': ('Due date missing', 'AP Operations'),
    'invalid_date': ('Invalid invoice date', 'AP Operations'),
    'invalid_due_date': ('Invalid due date', 'AP Operations'),
    'missing_items': ('No line items', 'AP Operations'),
    'missing_item': ('Line without item name', 'AP Operations'),
    'invalid_quantity': ('Non-positive quantity', 'AP Operations'),
    'invalid_price': ('Non-positive price', 'AP Operations'),
    'missing_total': ('Total missing', 'AP Operations'),
    'invalid_total': ('Non-positive total', 'AP Operations'),
    'invalid_charges': ('Invalid tax or shipping', 'AP Operations'),
    'line_mismatch': ('Line math mismatch', 'AP Operations'),
    'subtotal_mismatch': ('Subtotal mismatch', 'AP Operations'),
    'tax_mismatch': ('Tax mismatch', 'AP Operations'),
    'total_mismatch': ('Total mismatch', 'AP Operations'),
    'unknown_item': ('Unknown item', 'Procurement'),
    'out_of_stock': ('Out of stock', 'Inventory / Warehouse'),
    'stock_exceeded': ('Exceeds stock', 'Inventory / Warehouse'),
    'vendor_change': ('Vendor change', 'Vendor Management'),
    'unsupported_currency': ('Non-USD currency', 'Treasury'),
    'high_value': ('High value (>10K)', 'VP Finance'),
}
# Reasons that are an approval gate, not a defect. Everything else is a
# blocker that only corrected data (or a chosen version) can clear.
GATE_REASONS = {'high_value'}


def reason_label(code):
    return REASONS.get(code, (code.replace('_', ' ').capitalize(), 'AP Operations'))[0]


def reason_team(code):
    return REASONS.get(code, (None, 'AP Operations'))[1]


def order_reasons(codes):
    order = list(REASONS)
    return sorted(set(codes), key=lambda c: (order.index(c) if c in order else len(order), c))
