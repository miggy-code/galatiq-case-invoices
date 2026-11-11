"""All SQL: the schema, the catalog (the brief's inventory) and the payment ledger."""

import sqlite3

from .documents import amount_key


SCHEMA_VERSION = 5  # Payment claims retain the original numbers named by revisions.


# The brief's inventory. Unit prices are the catalog (POLICY decision 5), taken from the supplied invoices.
CATALOG = [('WidgetA', 15, '250'), ('WidgetB', 10, '500'), ('GadgetX', 5, '750'), ('FakeItem', 0, '1000')]


def open_database(path):
    conn = sqlite3.connect(path)
    existing = conn.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    if existing and conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f'Database schema is outdated; delete {path} and re-run')
    with conn:
        conn.execute(
            'CREATE TABLE IF NOT EXISTS inventory ('
            'item TEXT PRIMARY KEY, stock INTEGER NOT NULL, unit_price TEXT NOT NULL)'
        )
        conn.executemany('INSERT OR IGNORE INTO inventory VALUES (?,?,?)', CATALOG)
        conn.execute(
            'CREATE TABLE IF NOT EXISTS mock_payments ('
            'vendor TEXT, invoice_number TEXT, currency TEXT NOT NULL, amount TEXT NOT NULL, source TEXT NOT NULL, '
            'invoice_date TEXT, status TEXT NOT NULL, PRIMARY KEY(vendor,invoice_number))'
        )
        conn.execute(
            'CREATE TABLE IF NOT EXISTS payment_revisions ('
            'vendor TEXT, invoice_number TEXT, original_number TEXT, '
            'PRIMARY KEY(vendor,original_number))'
        )
        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    return conn


def inventory(conn):
    """Catalog rows as stored: (item, stock, unit_price)."""
    return conn.execute('SELECT item,stock,unit_price FROM inventory').fetchall()


def ledger_row(conn, identity):
    return conn.execute(
        'SELECT status, amount FROM mock_payments WHERE vendor=? AND invoice_number=?', identity
    ).fetchone()


def related_payment(conn, identity, originals=()):
    """Whether a named original or another revision already has a payment claim."""
    vendor, number = identity
    return any(ledger_row(conn, (vendor, original)) for original in originals) or any(
        conn.execute(
            'SELECT 1 FROM payment_revisions WHERE vendor=? AND original_number=?', (vendor, candidate)
        ).fetchone()
        for candidate in {number, *originals}
    )


def claim_payment(conn, identity, inv, originals=()):
    """Atomically claim this invoice and its named originals before calling the bank."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if related_payment(conn, identity, originals):
            return False
        cursor = conn.execute(
            "INSERT OR IGNORE INTO mock_payments VALUES (?,?,?,?,?,?,'pending')",
            (*identity, inv.currency, amount_key(inv.total), inv.source, inv.date),
        )
        if cursor.rowcount:
            conn.executemany(
                'INSERT INTO payment_revisions VALUES (?,?,?)',
                [(identity[0], identity[1], original) for original in originals],
            )
    return cursor.rowcount == 1


def mark_paid(conn, identity):
    with conn:
        conn.execute("UPDATE mock_payments SET status='paid' WHERE vendor=? AND invoice_number=?", identity)


def paid_under_another_number(conn, vendor, invoice_date, amount, number):
    """A payment with the same vendor, date and amount under a different invoice number."""
    return (
        conn.execute(
            'SELECT 1 FROM mock_payments WHERE vendor=? AND invoice_date=? AND amount=? AND invoice_number<>?',
            (vendor, invoice_date, amount, number),
        ).fetchone()
        is not None
    )


def number_paid_to_another_vendor(conn, number, vendor):
    return (
        conn.execute('SELECT 1 FROM mock_payments WHERE invoice_number=? AND vendor<>?', (number, vendor)).fetchone()
        is not None
    )
