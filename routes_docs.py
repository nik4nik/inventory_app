from datetime import datetime
from decimal import Decimal

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from sqlalchemy import select
from sqlalchemy.orm import joinedload

from models import db, Counterparty, Product, Warehouse, Document, DocumentLine, DocumentType
from inventory_service import InventoryService, InsufficientStockError
from utils import amount_to_ua_text, get_journal_docs, save_document_from_form

docs_bp = Blueprint('docs', __name__)

# --- ЖУРНАЛЫ ДОКУМЕНТОВ ---

@docs_bp.route('/all_documents')
def all_documents():
	stmt = select(Document).order_by(Document.date.desc(), Document.id.desc())
	docs = db.session.execute(stmt).scalars().all()
	return render_template('documents/all_documents.html', docs=docs)

@docs_bp.route('/journals/incoming')
def journal_incoming():
	docs = get_journal_docs(DocumentType.IN)
	return render_template('documents/journal.html', docs=docs, title="Прибуткові накладні", type='IN')

@docs_bp.route('/journals/outgoing')
def journal_outgoing():
	docs = get_journal_docs(DocumentType.OUT)
	return render_template('documents/journal.html', docs=docs, title="Видаткові накладні", type='OUT')

@docs_bp.route('/journals/orders')
def journal_orders():
	docs = get_journal_docs(DocumentType.ORDER)
	return render_template('documents/journal.html', docs=docs, title="Замовлення покупців", type='ORDER')

@docs_bp.route('/journals/invoices')
def journal_invoices():
	docs = get_journal_docs(DocumentType.INVOICE)
	return render_template('documents/journal.html', docs=docs, title="Рахунки-фактури", type='INVOICE')

# --- СОЗДАНИЕ ---

@docs_bp.route('/order/new', methods=['GET', 'POST'])
def create_order():
	if request.method == 'POST':
		new_doc = save_document_from_form(DocumentType.ORDER)
		db.session.commit()
		flash("Замовлення збережено", "success")
		return render_template('documents/document_details.html', doc=new_doc)

	return render_template('documents/create_order.html',
			counterparties = Counterparty.query.all(),
			products = Product.query.all())

@docs_bp.route('/invoice/new', methods=['GET', 'POST'])
def create_invoice():
	if request.method == 'POST':
		new_doc = save_document_from_form(DocumentType.INVOICE)
		db.session.commit()
		flash("Рахунок збережено", "success")
		return render_template('documents/document_details.html', doc=new_doc)

	return render_template('documents/create_invoice.html',
			counterparties = Counterparty.query.all(),
			products = Product.query.all())

@docs_bp.route('/incoming/new', methods=['GET', 'POST'])
def create_incoming():
	if request.method == 'POST':
		new_doc = save_document_from_form('IN')
		db.session.commit()
		# Возвращаем шаблон деталей, чтобы HTMX подставил его в центр экрана
		return render_template('documents/document_details.html', doc=new_doc)

	# GET запрос: показываем пустую форму
	warehouses = Warehouse.query.all()
	counterparties = Counterparty.query.all()
	products = Product.query.all()
	return render_template('documents/create_incoming.html',
			warehouses=warehouses,
			counterparties=counterparties,
			products=products)

@docs_bp.route('/outgoing/new', methods=['GET', 'POST'])
def create_outgoing():
	if request.method == 'POST':
		new_doc = save_document_from_form('OUT')
		db.session.commit()
		return render_template('documents/document_details.html', doc=new_doc)

	warehouses = Warehouse.query.all()
	counterparties = Counterparty.query.all()
	products = Product.query.all()
	return render_template('documents/create_outgoing.html',
			warehouses=warehouses,
			counterparties=counterparties,
			products=products)

# --- СОЗДАНИЕ НА ОСНОВАНИИ / РЕДАКТИРОВАНИЕ / ПРОВЕДЕНИЕ / УДАЛЕНИЕ ---

@docs_bp.route('/documents/<int:doc_id>/edit', methods=['GET', 'POST'])
def edit_document(doc_id):
	doc = db.session.get(Document, doc_id)
	if doc.is_posted:
		flash("Не можна редагувати проведений документ!", "warning")
		return render_template('documents/document_details.html', doc=doc)

	if request.method == 'POST':
		try:
			doc.date = datetime.strptime(request.form.get('date'), '%Y-%m-%d').date()
			doc.warehouse_id = request.form.get('warehouse_id') or None
			doc.counterparty_id = request.form.get('counterparty_id') or None
			doc.note = request.form.get('note')

			for line in list(doc.lines):
				db.session.delete(line)

			product_ids = request.form.getlist('product_id[]')
			quantities = request.form.getlist('quantity[]')
			prices = request.form.getlist('price[]')

			for p_id, qty, prc in zip(product_ids, quantities, prices):
				if p_id and qty:
					new_line = DocumentLine(
						document_id=doc.id,
						product_id=int(p_id),
						quantity=float(qty),
						price=float(prc)
					)
					db.session.add(new_line)

			db.session.commit()
			flash("Документ успішно оновлено", "success")
			return render_template('documents/document_details.html', doc=doc)

		except Exception as e:
			db.session.rollback()
			flash(f"Помилка при збереженні: {str(e)}", "danger")

	warehouses = db.session.execute(select(Warehouse)).scalars().all()
	counterparties = db.session.execute(select(Counterparty)).scalars().all()
	products = db.session.execute(select(Product)).scalars().all()

	return render_template('documents/edit_document.html', doc=doc, warehouses=warehouses,
						   counterparties=counterparties, products=products)

@docs_bp.route('/documents/<int:doc_id>/generate-next', methods=['POST'])
def create_from_parent(doc_id):
	parent_doc = db.session.get(Document, doc_id)
	if not parent_doc:
		return "Документ не знайдено", 404

	try:
		service = InventoryService(db.session)
		new_doc = None

		match parent_doc.doc_type:
			case DocumentType.ORDER:
				new_doc = service.create_invoice_from_order(doc_id)
			case DocumentType.INVOICE:
				new_doc = service.create_outgoing_from_invoice(doc_id)
			case DocumentType.OUT:
				new_doc = service.create_tax_from_outgoing(doc_id)
			case _:
				flash("Не вдалося створити документ на підставі поточного", "warning")
				return render_template('documents/document_details.html', doc=parent_doc)

		db.session.commit()
		flash(f"Документ {new_doc.number} створено на підставі №{parent_doc.number}", "success")
		return render_template('documents/document_details.html', doc=new_doc)

	except Exception as e:
		db.session.rollback()
		flash(f"Помилка при створенні: {str(e)}", "danger")
		# возвращаем старый документ, чтобы страница не "упала"
		return render_template('documents/document_details.html', doc=parent_doc)

@docs_bp.route('/document/post/<int:doc_id>', methods=['POST'])
def post_document(doc_id):
	doc = db.session.get(Document, doc_id)
	try:
		service = InventoryService(db.session)

		match doc.doc_type:
			case DocumentType.IN:
				service.post_incoming_invoice(doc.id)
			case DocumentType.OUT:
				service.post_outgoing_invoice(doc.id)
			case DocumentType.ORDER:
				service.post_order(doc.id)
			case DocumentType.INVOICE:
				service.post_invoice(doc.id)

		db.session.commit()
		flash(f"Документ №{doc.number} проведено", "success")
	except InsufficientStockError as e:
		db.session.rollback()
		msg = (f"Помилка проведення: Недостатньо '{e.product_name}' на складі '{e.warehouse_name}'. "
			   f"Потрібно: {e.required}, в наявності: {e.available}.")
		flash(msg, "danger")
	except Exception as e:
		db.session.rollback()
		flash(f"Помилка проведення: {str(e)}", "danger")

	return render_template('documents/document_details.html', doc=doc)

@docs_bp.route('/document/unpost/<int:doc_id>', methods=['POST'])
def unpost_document(doc_id):
	doc = db.session.get(Document, doc_id)
	try:
		service = InventoryService(db.session)

		match doc.doc_type:
			case DocumentType.IN:
				service.unpost_incoming_invoice(doc.id)
			case DocumentType.OUT:
				service.unpost_outgoing_invoice(doc.id)
			case DocumentType.ORDER:
				service.unpost_order(doc.id)
			case DocumentType.INVOICE:
				service.unpost_invoice(doc.id)

		db.session.commit()
		flash(f"Проведення скасовано", "warning")
	except Exception as e:
		db.session.rollback()
		flash(f"Помилка скасування: {str(e)}", "danger")

	return render_template('documents/document_details.html', doc=doc)

@docs_bp.route('/document/delete/<int:doc_id>', methods=['DELETE', 'POST'])
def delete_document(doc_id):
	doc = db.session.get(Document, doc_id)
	if not doc or doc.is_posted:
		flash("Неможливо видалити документ", "danger")
		return render_template('documents/document_details.html', doc=doc)

	type_to_journal = {
		DocumentType.ORDER: ('Замовлення клієнтів', 'ORDER'),
		DocumentType.INVOICE: ('Рахунки-фактури', 'INVOICE'),
		DocumentType.IN:	('Прибуткові накладні', 'IN'),
		DocumentType.OUT:	('Видаткові накладні', 'OUT'),
	}
	title, d_type = type_to_journal.get(doc.doc_type)

	db.session.delete(doc)
	db.session.commit()
	flash("Документ видалено", "success")

	# После удаления возвращаем журнал (список документов этого типа)
	stmt = select(Document).where(Document.doc_type == d_type).order_by(Document.date.desc())
	docs = db.session.execute(stmt).scalars().all()
	return render_template('documents/journal.html',
			title=title,
			docs=docs,
			type=d_type.name
		)

@docs_bp.route('/document/<int:doc_id>')
def document_detail(doc_id):
	# Подгружаем связанные данные одним запросом
	doc = db.session.query(Document).options(
		joinedload(Document.lines),
		joinedload(Document.counterparty),
		joinedload(Document.warehouse)
	).get(doc_id)

	if not doc:
		return "Документ не знайдено", 404
	return render_template('documents/document_details.html', doc=doc)

@docs_bp.route('/document/<int:doc_id>/print')
def print_document(doc_id):
	"""
	Универсальная функция печати для всех типов документов.
	"""
	# Загружаем документ со всеми связями один раз
	doc = db.session.query(Document).options(
		joinedload(Document.lines).joinedload(DocumentLine.product),
		joinedload(Document.counterparty),
		joinedload(Document.warehouse)
	).get_or_404(doc_id)

	# Считаем итоги, сразу переводя в Decimal для точности, а затем в float для шаблона
	total_sum_decimal = sum((line.quantity * line.price) for line in doc.lines)
	vat_amount_decimal = total_sum_decimal * Decimal('0.2')

	# Добавляем атрибуты в объект, чтобы шаблоны их видели
	doc.total_sum = float(total_sum_decimal)
	doc.vat_amount = float(vat_amount_decimal)

	# Сумма прописью
	doc.total_text = amount_to_ua_text(total_sum_decimal)
	doc.vat_text = amount_to_ua_text(vat_amount_decimal)

	# Предварительный расчет каждой строки для шаблона
	for line in doc.lines:
		line.row_total = float(line.quantity * line.price)

	templates_map = {
		DocumentType.IN: 'print_incoming.html',
		DocumentType.OUT: 'print_outgoing.html',
		DocumentType.TAX: 'print_tax_invoice.html'
	}

	template_name = templates_map.get(doc.doc_type)

	if not template_name:
		return f"Друк для типу '{doc.doc_type}' ще не налаштований", 404

	return render_template(template_name, doc=doc)