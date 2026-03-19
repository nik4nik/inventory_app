from __future__ import annotations
from decimal import Decimal
import datetime
import re

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from models import Batch, db, Document, DocumentLine, DocumentType, SalesAccumulator


class InsufficientStockError(Exception):
	"""Недостаточно товара на складе для списания"""

	def __init__(self,
			product_id: int,
			product_name: str,
			warehouse_id: int,
			warehouse_name: str,
			required: Decimal,
			available: Decimal) -> None:
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

class InventoryService:
	"""
	Сервис проведения складских документов.

	Принимает SQLAlchemy-сессию через конструктор (dependency injection),
	что упрощает тестирование и позволяет управлять транзакцией снаружи.
	"""

	def __init__(self, _session: Session) -> None:
		self._session = _session

	def create_document(self, doc_type, **kwargs):
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

		# Получить число из строки, например, из "ЗП-005" получим 5
		match = re.search(r'(\d+)$', last_doc.number)
		if match:
			next_num = int(match.group(1)) + 1
			return f"{prefix}-{next_num:03d}"

		return f"{prefix}-001"

# Проведение

	def post_incoming_invoice(self, document_id: int):
		"""
		Провести приходную накладную.
		Для каждой строки-товара(не услуги) создаёт запись Batch.
		"""
		doc = self._session.get(Document, document_id)

		for line in doc.lines:
			if line.product.is_service:
				continue

			# Существует ли уже партия для этой строки?
			existing_batch = self._session.scalar(
				select(Batch).where(Batch.incoming_line_id == line.id)
			)
			if existing_batch:
				# Если партия есть, обновляем её, на случай изменений в строке
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

	def post_simple(self, document_id: int):
		"""Встановлює статус проведеності"""

		doc = self._session.get(Document, document_id)
		if not doc.is_posted:
			doc.is_posted = True
			self._session.commit()

# Отмена проведения

	def unpost_incoming_invoice(self, doc_id):
		"""Отмена прихода: удаление партий"""
		doc = self._session.get(Document, doc_id)
		if not doc:
			raise ValueError("Документ не знайдено")

		if not doc.is_posted:
			return doc

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

		# Берем из регистра движения документа
		movements = self._session.execute(
			select(SalesAccumulator).where(SalesAccumulator.document_id == document_id)
		).scalars().all()

		# Возвращаем остатки в те партии, откуда они пришли
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

	def unpost_simple(self, document_id: int):
		"""Скасувати проведення"""

		doc = self._session.get(Document, document_id)
		if doc.is_posted:
			doc.is_posted = False
			self._session.commit()

# Создание на основании

	def create_invoice_from_parent(self, doc_id: int, new_doc_type: DocumentType) -> Document:
		parent = self._session.get(Document, doc_id)
		if not parent:
			raise ValueError("Вихідний документ не знайдено")

		new_doc = Document(
			doc_type=new_doc_type,
			parent_id=parent.id,
			counterparty_id=parent.counterparty_id,
			warehouse_id=parent.warehouse_id,
			date=datetime.datetime.now(),
			note=f"Створено на підставі {parent.number}"
		)
		for line in parent.lines:
			new_line = DocumentLine(
				document_id=new_doc.id,
				product_id=line.product_id,
				quantity=line.quantity,
				price=line.price
			)
			new_doc.lines.append(new_line)

		return new_doc