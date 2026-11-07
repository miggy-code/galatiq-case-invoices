from __future__ import annotations

import csv
import json
import re
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from pypdf.errors import PdfReadError
import xml.etree.ElementTree as ET


@dataclass
class Line:
    item: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal | None = None


@dataclass
class Invoice:
    source: str
    number: str
    vendor: str
    date: str | None
    due_date: str | None
    lines: list[Line]
    total: Decimal | None
    subtotal: Decimal | None = None
    tax: Decimal = Decimal('0')
    tax_rate: Decimal | None = None
    shipping: Decimal = Decimal('0')
    currency: str = 'USD'
    revision: str = ''
    notes: str = ''
    corrections: list[str] = field(default_factory=list)


def number(value, corrections):
    text = str(value).strip().replace('$', '').replace(',', '')
    if re.fullmatch(r'[+\-]?[\dOo]+(?:\.[\dOo]+)?', text) and re.search(r'\d',text):
        corrected = text.replace('O','0').replace('o','0')
        if corrected != text:
            corrections.append(f'numeric OCR: {text} -> {corrected}')
        text = corrected
    result = Decimal(text)
    if not result.is_finite():
        raise ValueError('Non-finite number')
    return result


def item_key(value):
    return re.sub(r'[^a-z0-9]', '', re.sub(r'\([^)]*\)', '', value).lower())


def vendor_key(value):
    words = re.findall(r'[a-z0-9]+', value.lower().replace('&',' and '))
    aliases = {'incorporated':'inc','company':'co','corporation':'corp','limited':'ltd'}
    return ' '.join(aliases.get(w,w) for w in words)


def invoice_key(value):
    text = re.sub(r'[^A-Z0-9]', '', value.upper())
    return text[3:] if text.startswith('INV') else text


def parsed_date(value, corrections):
    if not value:
        return None
    text = str(value).strip()
    fixed = re.sub(r'(?<=\d)[Oo](?=\d)', '0', text)
    if fixed != text:
        corrections.append(f'date OCR: {text} -> {fixed}')
    for fmt in ('%Y-%m-%d','%m/%d/%Y','%b %d %Y','%B %d, %Y','%d-%b-%Y'):
        try:
            return datetime.strptime(fixed,fmt).date().isoformat()
        except ValueError:
            pass
    return text  # Preserve unparseable evidence for a reject diagnostic.


def from_mapping(data, source):
    corrections=[]
    def dec(key, default=None):
        val=data.get(key,default)
        return None if val is None else number(val,corrections)
    vendor=data.get('vendor','')
    if isinstance(vendor,dict):
        vendor=vendor.get('name','')
    lines=[]
    for row in data.get('line_items',[]):
        lines.append(Line(str(row.get('item','')),number(row['quantity'],corrections),
                          number(row['unit_price'],corrections),
                          None if row.get('amount') is None else number(row['amount'],corrections)))
    return Invoice(source,str(data.get('invoice_number','')),str(vendor or ''),
                   parsed_date(data.get('date'),corrections),parsed_date(data.get('due_date'),corrections),
                   lines,dec('total'),dec('subtotal'),dec('tax_amount',0),dec('tax_rate'),dec('shipping',0),
                   str(data.get('currency','USD')).upper(),str(data.get('revision','')),
                   str(data.get('notes','')),corrections)


def csv_mapping(text):
    rows=list(csv.reader(text.splitlines()))
    if not rows:
        raise ValueError('Empty CSV')
    result={'line_items':[]}
    if rows[0]==['field','value']:
        current=None
        for key,value in rows[1:]:
            if key=='item':
                current={'item':value}
                result['line_items'].append(current)
            elif key in ('quantity','unit_price'):
                if current is None:
                    raise ValueError('CSV quantity/price before item')
                current[key]=value
            else:
                result['tax_amount' if key=='tax' else key]=value
    else:
        for row in csv.DictReader(text.splitlines()):
            if row.get('Invoice Number'):
                for key,col in [('invoice_number','Invoice Number'),('vendor','Vendor'),('date','Date'),('due_date','Due Date')]:
                    if key in result and result[key]!=row[col]:
                        raise ValueError('Mixed invoice identities in CSV')
                    result[key]=row[col]
                result['line_items'].append({'item':row['Item'],'quantity':row['Qty'],
                                             'unit_price':row['Unit Price'],'amount':row['Line Total']})
            else:
                label=row.get('Unit Price','').lower()
                key='subtotal' if label.startswith('subtotal') else 'tax_amount' if label.startswith('tax') else 'total' if label.startswith('total') else None
                if key is None:
                    raise ValueError('Unrecognized CSV summary')
                result[key]=row['Line Total']
                rate=re.search(r'([\d.]+)%',label)
                if rate:
                    result['tax_rate']=str(Decimal(rate[1])/100)
    return result


def text_mapping(text):
    result={'line_items':[], 'notes':text}
    # shortcut: handles the supplied text-based layouts, not arbitrary scans; add
    # a separate extraction stage when new vendor layouts or image PDFs arrive.
    labels={
        'vendor':r'(?:Vendor|Vndr)',
        'invoice_number':r'(?:Invoice Number|Invoice|Inv #|INV NO)',
        'date':r'(?:Date|Dt)',
        'due_date':r'(?:Due Date|Due Dt|Due|DUE)',
    }
    for key,label in labels.items():
        match=re.search(rf'^\s*{label}\s*:\s*(.+?)(?=\s{{2,}}(?:Date|Due):|$)',text,re.I|re.M)
        if match:
            result[key]=match[1].strip()
    if 'vendor' not in result:
        match=re.search(r'^\s*FROM:\s*(.+)$',text,re.I|re.M)
        if match:
            result['vendor']=match[1].strip()
    if 'invoice_number' not in result:
        match=re.search(r'\b(?:INV[- ]?\d+)\b',text,re.I)
        if match:
            result['invoice_number']=match[0]
    # PDF headers can contain Date/Due on the same row as another field.
    for key,label in [('date','Date'),('due_date','Due')]:
        if key not in result:
            match=re.search(rf'\b{label}:\s*(\d{{4}}-\d{{2}}-\d{{2}})',text,re.I)
            if match:
                result[key]=match[1]
    numeric=r'[+\-]?[\dOo][\dOo,]*(?:\.[\dOo]+)?'
    money=rf'\$?{numeric}'
    for raw in text.splitlines():
        line=raw.strip()
        # Totals are matched from start of line to avoid subtotal/line confusion.
        match=re.fullmatch(rf'(Subtotal|Sales Tax|Tax(?:\s*\([\d.]+%\))?|Shipping|Grand Total|Total Amount|Total|Amt)\s*:\s*({money})',line,re.I)
        if match:
            label=match[1].lower()
            key='subtotal' if label=='subtotal' else 'tax_amount' if 'tax' in label else 'shipping' if label=='shipping' else 'total'
            result[key]=match[2]
            rate=re.search(r'([\d.]+)%',label)
            if rate:
                result['tax_rate']=str(Decimal(rate[1])/100)
            continue
        # qty / @ layouts, email x12 layouts, and aligned table rows.
        patterns=[
            rf'(.+?)\s+qty:?\s*({numeric})\s+(?:unit price:\s*|@\s*)({money})(?:\s+ea)?',
            rf'-?\s*(.+?)\s+x({numeric})\s+({money})\s+each',
            rf'(.+?)\s{{2,}}({numeric})\s+({money})\s+({money})(?:\s+.*)?',
        ]
        for pattern in patterns:
            match=re.fullmatch(pattern,line,re.I)
            if match:
                item,quantity,price=match.groups()[:3]
                row={'item':item.strip(),'quantity':quantity,'unit_price':price}
                if len(match.groups())==4:
                    row['amount']=match[4]
                result['line_items'].append(row)
                break
    return result


def load_invoice(path):
    path=Path(path)
    suffix=path.suffix.lower()
    if suffix=='.json':
        data=json.loads(path.read_text(),parse_float=Decimal)
    elif suffix=='.csv':
        data=csv_mapping(path.read_text())
    elif suffix=='.xml':
        root=ET.fromstring(path.read_text())
        data={child.tag:child.text for group in ('header','totals') for child in root.findall(f'{group}/*')}
        data['line_items']=[{('item' if c.tag=='name' else c.tag):c.text for c in row} for row in root.findall('line_items/item')]
    elif suffix in ('.txt','.pdf'):
        if suffix=='.pdf':
            from pypdf import PdfReader
            text='\n'.join(p.extract_text(extraction_mode='layout') or '' for p in PdfReader(path).pages)
        else:
            text=path.read_text()
        data=text_mapping(text)
    else:
        raise ValueError(f'Unsupported format: {suffix}')
    if not isinstance(data,dict):
        raise ValueError('Invoice must be an object')
    return from_mapping(data,path.name)


def open_database(path):
    conn=sqlite3.connect(path)
    with conn:
        conn.execute('CREATE TABLE IF NOT EXISTS inventory (item TEXT PRIMARY KEY, stock INTEGER NOT NULL)')
        conn.executemany('INSERT OR IGNORE INTO inventory VALUES (?,?)', [('WidgetA',15),('WidgetB',10),('GadgetX',5),('FakeItem',0)])
        conn.execute('CREATE TABLE IF NOT EXISTS mock_payments (vendor TEXT, invoice_number TEXT, currency TEXT NOT NULL, amount TEXT NOT NULL, source TEXT NOT NULL, PRIMARY KEY(vendor,invoice_number))')
    return conn


def evaluate(inv, conn):
    errors=[]
    holds=[]
    quantities=defaultdict(Decimal)
    def close(a,b):
        return abs(a-b)<=Decimal('.01')
    for key,value in [('vendor',inv.vendor),('invoice_number',inv.number),('date',inv.date),('due_date',inv.due_date)]:
        if not value or not str(value).strip():
            errors.append('missing_'+key)
    for key,value in [('date',inv.date),('due_date',inv.due_date)]:
        if value:
            try:
                datetime.strptime(value,'%Y-%m-%d')
            except ValueError:
                errors.append('invalid_'+key)
    if not inv.lines:
        errors.append('missing_items')
    subtotal=Decimal('0')
    for line in inv.lines:
        if not line.item.strip():
            errors.append('missing_item')
        if line.quantity<=0:
            errors.append('invalid_quantity')
        if line.unit_price<=0:
            errors.append('invalid_price')
        amount=(line.quantity*line.unit_price).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)
        subtotal+=amount
        quantities[item_key(line.item)]+=line.quantity
        if line.amount is not None and not close(line.amount,amount):
            errors.append('line_mismatch')
    if inv.subtotal is not None and not close(inv.subtotal,subtotal):
        errors.append('subtotal_mismatch')
    if inv.tax<0 or inv.shipping<0 or (inv.tax_rate is not None and not 0<=inv.tax_rate<=1):
        errors.append('invalid_charges')
    if inv.tax_rate is not None and not close(inv.tax,(subtotal*inv.tax_rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)):
        errors.append('tax_mismatch')
    if inv.total is None:
        errors.append('missing_total')
    else:
        if inv.total<=0:
            errors.append('invalid_total')
        if not close(inv.total,(inv.subtotal if inv.subtotal is not None else subtotal)+inv.tax+inv.shipping):
            errors.append('total_mismatch')
        if inv.currency=='USD' and inv.total>10000:
            holds.append('high_value')
    stocks={item_key(name):(name,stock) for name,stock in conn.execute('SELECT item,stock FROM inventory')}
    display={}
    for item,qty in quantities.items():
        canonical,stock=stocks.get(item,(next(l.item for l in inv.lines if item_key(l.item)==item),None))
        display[canonical]=int(qty) if qty==int(qty) else str(qty)
        if stock is None:
            holds.append('unknown_item')
        elif stock<=0:
            holds.append('out_of_stock')
        elif qty>stock:
            holds.append('stock_exceeded')
    if inv.currency!='USD':
        holds.append('unsupported_currency')
    if re.search(r'\bformerly\b|\bnow part of\b',inv.notes,re.I):
        holds.append('vendor_change')
    return {'source':inv.source,'invoice_number':inv.number,'vendor':inv.vendor,
            'decision':'reject' if errors else 'hold' if holds else 'approve',
            'reasons':sorted(set(errors+holds)), 'total':str(inv.total) if inv.total is not None else None,
            'currency':inv.currency,'date':inv.date,'due_date':inv.due_date,
            'quantities':display,'corrections':inv.corrections}


def economic_content(inv):
    # Compare computed economics so absent summary fields in a PDF are harmless.
    lines=sorted((item_key(l.item),str(l.quantity.normalize()),str(l.unit_price.normalize()),
                  str((l.amount if l.amount is not None else l.quantity*l.unit_price).normalize())) for l in inv.lines)
    return (inv.date, inv.due_date, inv.currency, inv.revision, inv.total, inv.tax, inv.shipping, lines)


def process_batch(inputs, conn, *, record_payments=False):
    results=[]
    groups=defaultdict(list)
    for source in inputs:
        try:
            inv=source if isinstance(source,Invoice) else load_invoice(source)
        except (ValueError,KeyError,TypeError,AttributeError,InvalidOperation,ET.ParseError,PdfReadError) as exc:
            results.append({'source':Path(source).name,'decision':'reject','reasons':['parse_error'],'detail':str(exc)})
            continue
        result=evaluate(inv,conn)
        results.append(result)
        if inv.vendor.strip() and invoice_key(inv.number):
            groups[(vendor_key(inv.vendor),invoice_key(inv.number))].append((inv,result))
    # Resolve the entire batch before recording any mock payment.
    for identity,entries in groups.items():
        entries.sort(key=lambda pair:pair[0].source)
        first=entries[0][0]
        conflict=any(economic_content(inv)!=economic_content(first) or
                     result['reasons']!=entries[0][1]['reasons']
                     for inv,result in entries[1:])
        if conflict:
            for _,result in entries:
                result['reasons']=sorted(set(result['reasons']+['revision_conflict']))
                if result['decision']!='reject':
                    result['decision']='hold'
            continue
        for _,result in entries[1:]:
            result['decision']='duplicate'
            result['reasons']=sorted(set(result['reasons']+['duplicate_document']))
        inv,result=entries[0]
        if record_payments and result['decision']=='approve':
            with conn:
                cursor=conn.execute('INSERT OR IGNORE INTO mock_payments VALUES (?,?,?,?,?)',
                                    (*identity,inv.currency,str(inv.total),inv.source))
                if cursor.rowcount==0:
                    result['decision']='duplicate'
                    result['reasons']=['already_recorded']
            result['mock_payment_recorded']=cursor.rowcount==1
    return results
