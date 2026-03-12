from num2words import num2words
from sqlalchemy import func, select
from sqlalchemy.orm import joinedload

from models import db, Document, DocumentLine


def save_document_from_form(doc_type):
	"""
    Створює новий документ у стані чернетки на основі даних із HTTP-запиту.
    
    Функція автоматично генерує номер документа, якщо він не вказаний, 
    зчитує заголовок (дата, контрагент, склад) та додає специфікацію (рядки) товарів.
    
    Args:
        doc_type (DocumentType): Тип документа для створення (IN, OUT, ORDER, INVOICE).
        
    Returns:
        Document: Створений об'єкт документа з прив'язаними рядками.
    """
	service = InventoryService(db.session)
	form_number = request.form.get('number')
	if not form_number or form_number.strip() == "":
		form_number = service.generate_next_number(doc_type)

	date_str = request.form.get('date')
	doc_date = datetime.strptime(date_str, '%Y-%m-%d').date() if date_str else datetime.now().date()

	new_doc = Document(
		number=form_number,
		date=doc_date,
		doc_type=doc_type,
		counterparty_id=request.form.get('counterparty_id') or None,
		warehouse_id=request.form.get('warehouse_id') or None,
		is_posted=False
	)
	db.session.add(new_doc)
	db.session.flush()

	product_ids = request.form.getlist('product_id[]')
	quantities = request.form.getlist('quantity[]')
	prices = request.form.getlist('price[]')

	for p_id, qty, prc in zip(product_ids, quantities, prices):
		if not p_id or not qty: continue
		line = DocumentLine(
			document_id=new_doc.id,
			product_id=int(p_id),
			quantity=float(qty),
			price=float(prc)
		)
		db.session.add(line)
	return new_doc

def get_journal_docs(doc_type):
	"""Загружает документы с контрагентами и суммами одним запросом"""

	sum_of_lines = (
		select(
			DocumentLine.document_id,
			func.sum(DocumentLine.quantity * DocumentLine.price).label('total_sum')
		)
		.group_by(DocumentLine.document_id)
	).subquery()

	stmt = (
		select(Document, sum_of_lines.c.total_sum)
		.outerjoin(sum_of_lines, Document.id == sum_of_lines.c.document_id)
		.options(joinedload(Document.counterparty))
		.where(Document.doc_type == doc_type)
		.order_by(Document.date.desc(), Document.id.desc())
	)

	results = db.session.execute(stmt).all()

	docs = []
	for doc, total in results:
		doc.total_sum = total or 0
		docs.append(doc)
	return docs

def amount_to_ua_text(amount):
	"""Превращает Decimal/float в строку: 'Двісті тридцять чотири гривні 00 копійок'"""
	units = int(amount)
	cents = int(round((amount - units) * 100))

	# Генерируем текст для гривен
	words = num2words(units, lang='uk')

	# Логика окончаний для слова "гривня"
	last_digit = units % 10
	last_two_digits = units % 100

	if 11 <= last_two_digits <= 14:
		currency = "гривень"
	elif last_digit == 1:
		currency = "гривня"
	elif 2 <= last_digit <= 4:
		currency = "гривні"
	else:
		currency = "гривень"

	return f"{words} {currency} {cents:02d} копійок".capitalize()