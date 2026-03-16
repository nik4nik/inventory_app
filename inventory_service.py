from __future__ import annotations
from decimal import Decimal
import datetime
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from models import Batch, db, Document, DocumentLine, DocumentType, SalesAccumulator


class InsufficientStockError(Exception):
	"""Недостаточно товара на складе для списания"""

	def __init__(self, product_id: int, product_name: str,
				warehouse_id: int, warehouse_name: str,
				required: Decimal, available: Decimal) -> None:
		self.product_id = product_id
		self.warehouse_name = warehouse_name
		self.product_name = product_name
		self.required = required
		self.available = available
		shortage = required - available
		super().__init__(
			f"Недостаточно товара '{product_name}' (id={product_id}) на складе '{warehouse_name}' (id={warehouse_id}): "
			f"требуется {required}, доступно {available}, "
			f"не хватает {shortage}."
		)

class DocumentAlreadyPostedError(Exception):
	"""Попытка перепровести уже проведённый документ"""

class InventoryService:
	"""
	Сервис проведения складских документов.

	Принимает SQLAlchemy-сессию через конструктор (dependency injection),
	что упрощает тестирование и позволяет управлять транзакцией снаружи.
	"""

	def __init__(self, _session: Session) -> None:
		self._session = _session

	def create_document(self, doc_type, **kwargs):
		# Если номер не передан - генерируем
		if 'number' not in kwargs:
			kwargs['number'] = self.generate_next_number(doc_type)

		new_doc = Document(doc_type=doc_type, **kwargs)
		self._session.add(new_doc)
		return new_doc

	def generate_next_number(self, doc_type: DocumentType) -> str:
		prefixes = {
			DocumentType.ORDER: "ЗП",	# Замовлення покупця
			DocumentType.INVOICE: "РФ",	# Рахунок-фактура
			DocumentType.IN: "ПН",		# Прибуткова
			DocumentType.OUT: "ВН",		# Видаткова
			DocumentType.TAX: "НН"		# Податкова
		}
		prefix = prefixes.get(doc_type, "ДОК")

		# Ищем последний документ этого типа
		last_doc = self._session.execute(
			select(Document)
			.where(Document.doc_type == doc_type)
			.order_by(Document.id.desc())
			.limit(1)
		).scalar()

		if not last_doc or not last_doc.number:
			return f"{prefix}-001"

		# Извлекаем число из строки (например, из "ЗП-005" достаем 5)
		match = re.search(r'(\d+)$', last_doc.number)
		if match:
			next_num = int(match.group(1)) + 1
			return f"{prefix}-{next_num:03d}" # Форматируем как 001, 002...

		return f"{prefix}-001"

# Проведение

	def _check_not_posted(self, doc: Document) -> None:
		"""Запретить перепроведение."""
		if doc.is_posted:
			raise DocumentAlreadyPostedError(
				f"Документ №{doc.number} (id={doc.id}) уже проведён. "
				"Перепроведение запрещено. Сначала отмените документ."
			)

	def post_incoming_invoice(self, document_id: int):
		"""
		Провести приходную накладную.
		Для каждой строки-товара(не услуги) создаёт запись Batch.
		"""
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc)

		for line in doc.lines:
			if line.product.is_service:
				continue

			# Существует ли уже партия для этой строки?
			existing_batch = self._session.scalar(
				select(Batch).where(Batch.incoming_line_id == line.id)
			)
			if existing_batch:
				# Если партия есть, обновляем её (на случай изменений в строке)
				existing_batch.current_quantity = line.quantity
				existing_batch.initial_quantity = line.quantity
				existing_batch.purchase_price = line.price
			else:
				# Создаем новую партию
				batch = Batch(
					warehouse_id	 = doc.warehouse_id,
					incoming_line_id = line.id,
					product_id		 = line.product_id,
					purchase_price	 = line.price,
					initial_quantity = line.quantity,
					current_quantity = line.quantity,
				)
				self._session.add(batch)

		doc.is_posted = True
		self._session.commit()

	def post_outgoing_invoice(self, document_id: int):
		"""
		Провести расходную накладную.

		Для каждой строки-товара:
		  - проверяет достаточность остатков (иначе InsufficientStockError),
		  - списывает по FIFO
		"""

		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc)
		if not doc:
			raise ValueError("Документ не знайдено")

		# проверка заполнения реквизитов шапки
		errors = []
		if not doc.warehouse_id:
			errors.append("не вказано склад")
		if not doc.counterparty_id:
			errors.append("не вказано контрагента")

		if errors:
			error_msg = "Документ неможливо провести: " + ", ".join(errors) + "."
			raise ValueError(error_msg)

		for line in doc.lines:

			# УСЛУГИ: запись без списания со склада
			if line.product.is_service:
				entry = SalesAccumulator(
					date=doc.date,
					document_id=doc.id,
					product_id=line.product_id,
					counterparty_id=doc.counterparty_id,
					warehouse_id=doc.warehouse_id,
					quantity=line.quantity,
					sale_price=line.price,
					total_sale_sum=line.quantity * line.price,
					total_cost_sum=0,
					batch_id=None # У услуг нет партии
				)
				self._session.add(entry)
				continue

			# ТОВАРЫ
			qty_to_ship = line.quantity

			# Поиск партий по FIFO
			stmt = (
				select(Batch)
				.where(
					Batch.product_id == line.product_id,
					Batch.warehouse_id == doc.warehouse_id,
					Batch.current_quantity > 0
				)
				.order_by(Batch.created_at.asc())
			)
			batches = self._session.execute(stmt).scalars().all()

			# Проверка остатка
			total_available = sum(b.current_quantity for b in batches)
			if total_available < qty_to_ship:
				raise InsufficientStockError(
					line.product_id, line.product.name,
					doc.warehouse_id, doc.warehouse.name,
					line.quantity, total_available
				)

			# Списание по партиям
			for batch in batches:
				if qty_to_ship <= 0:
					break

				can_take = min(batch.current_quantity, qty_to_ship)

				# Создаем движение для найденной части партии
				entry = SalesAccumulator(
					date=doc.date,
					document_id=doc.id,
					product_id=line.product_id,
					counterparty_id=doc.counterparty_id,
					warehouse_id=doc.warehouse_id,
					batch_id=batch.id, # привязка к партии
					quantity=can_take,
					sale_price=line.price,
					total_sale_sum=can_take * line.price,
					total_cost_sum=can_take * batch.purchase_price
				)
				self._session.add(entry)

				# Уменьшаем остаток партии
				batch.current_quantity -= can_take
				qty_to_ship -= can_take

		doc.is_posted = True
		self._session.commit()

	def post_order(self, document_id: int):
		"""
		Провести замовлення покупця.
		Тільки змінює статус проведеності.
		"""
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc) # Проверка, что документ не проведен

		doc.is_posted = True
		self._session.commit()

	def post_invoice(self, document_id: int):
		"""
		Провести рахунок-фактуру.
		Тільки змінює статус проведеності.
		"""
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc)

		doc.is_posted = True
		self._session.commit()

	def post_tax(self, document_id: int):
		"""
		Провести податкову.
		Тільки змінює статус проведеності.
		"""
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc)

		doc.is_posted = True
		self._session.commit()

# Отмена проведения

	def unpost_incoming_invoice(self, doc_id):
		"""Отмена прихода: удаление партий"""
		doc = self._session.get(Document, doc_id)
		if not doc:
			raise ValueError("Документ не знайдено")

		if not doc.is_posted:
			return doc # Уже не проведен

		for line in doc.lines:
			if line.batch:
				self._session.delete(line.batch)

		doc.is_posted = False
		self._session.commit()

	def unpost_outgoing_invoice(self, document_id: int):
		"""
		Отменить проведение расходной накладной.
		"""

		doc = self._session.get(Document, document_id)
		if not doc or not doc.is_posted:
			return doc

		# Берем из регистра все движения документа
		movements = self._session.execute(
			select(SalesAccumulator).where(SalesAccumulator.document_id == document_id)
		).scalars().all()

		# Возвращаем остатки точно в те партии, откуда они пришли
		for move in movements:
			if move.batch_id:
				batch = self._session.get(Batch, move.batch_id)
				if batch:
					batch.current_quantity += move.quantity

		# Очищаем регистр
		self._session.execute(
			delete(SalesAccumulator).where(SalesAccumulator.document_id == document_id)
		)

		doc.is_posted = False
		self._session.commit()

	def unpost_order(self, document_id: int):
		"""
		Скасувати проведення замовлення.
		"""
		doc = self._session.get(Document, document_id)
		if not doc.is_posted:
			return # Уже не проведен

		doc.is_posted = False
		self._session.commit()

	def unpost_invoice(self, document_id: int):
		"""
		Скасувати проведення рахунку.
		"""
		doc = self._session.get(Document, document_id)
		if not doc.is_posted:
			return

		doc.is_posted = False
		self._session.commit()

	def unpost_tax(self, document_id: int):
		"""
		Скасувати проведення податкової.
		"""
		doc = self._session.get(Document, document_id)
		if not doc.is_posted:
			return

		doc.is_posted = False
		self._session.commit()

# Создание на основании

	def _create_from_parent(self, parent_id: int, target_type: DocumentType) -> Document:
		# Получаем исходный документ
		parent = self._session.get(Document, parent_id)
		if not parent:
			raise ValueError("Вихідний документ не знайдено")

		# Создаем новый заголовок
		new_doc = Document(
			number=self.generate_next_number(target_type),
			date=datetime.date.today(),
			doc_type=target_type,
			parent_id=parent.id,
			is_posted=False,
			warehouse_id=parent.warehouse_id,
			counterparty_id=parent.counterparty_id,
			note=f"Створено на підставі {parent.number}"
		)
		# объект из состояния Transient (объект в памяти)
		# переходит в состояние Pending (в очереди на запись)
		self._session.add(new_doc)
		# Поле id генерируется базой данных (Auto-increment),
		# выполним вставку, чтобы база вернула сгенерированный номер
		# invoice.id перестает быть None и получает реальное значение
		self._session.flush()

		# Копируем строки
		for line in parent.lines:
			new_line = DocumentLine(
				document_id=new_doc.id,
				product_id=line.product_id,
				quantity=line.quantity,
				price=line.price
			)
			self._session.add(new_line)

		return new_doc

	def create_invoice_from_order(self, order_id: int) -> Document:
		order = self._session.get(Document, order_id)
		new_doc = Document(
			doc_type=DocumentType.INVOICE,
			parent_id=order.id,
			counterparty_id=order.counterparty_id,
			warehouse_id=order.warehouse_id,
			date=datetime.datetime.now(),
			# номер пока не присваиваем, если он генерируется при сохранении
		)
		for line in order.lines:
			new_line = DocumentLine(
				product_id=line.product_id,
				quantity=line.quantity,
				price=line.price
			)
			new_doc.lines.append(new_line)

		return new_doc

	def create_outgoing_from_invoice(self, invoice_id: int) -> Document:
		invoice = self._session.get(Document, invoice_id)
		new_doc = Document(
			doc_type=DocumentType.OUT,
			parent_id=invoice.id,
			counterparty_id=invoice.counterparty_id,
			warehouse_id=invoice.warehouse_id,
			date=datetime.datetime.now(),
			# номер пока не присваиваем, если он генерируется при сохранении
		)
		for line in invoice.lines:
			new_line = DocumentLine(
				product_id=line.product_id,
				quantity=line.quantity,
				price=line.price
			)
			new_doc.lines.append(new_line)

		return new_doc

	def create_tax_from_outgoing(self, outgoing_id) -> Document:
		outgoing = self._session.get(Document, outgoing_id)
		new_tax = Document(
			doc_type=DocumentType.TAX,
			parent_id=outgoing.id,
			date=datetime.datetime.now(),
			counterparty_id=outgoing.counterparty_id,
			warehouse_id=outgoing.warehouse_id
		)
		for line in outgoing.lines:
			new_line = DocumentLine(
				product_id=line.product_id,
				quantity=line.quantity,
				price=line.price
			)
			new_tax.lines.append(new_line)

		return new_tax