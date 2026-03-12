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
		  - списывает по FIFO,
		  - сохраняет себестоимость в line.cost_price.
		Услуги пропускаются.
		"""

		# Получаем объект документа, чтобы узнать склад (Warehouse)
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc)

		if not doc:
			raise ValueError("Документ не знайдено")

		# Работаем со списком строк, так как doc.lines может меняться
		lines_to_process = list(doc.lines)

		for line in lines_to_process:
			if line.product.is_service:
				continue

			qty_to_ship = line.quantity

			# 2. Ищем партии по FIFO на конкретном складе
			stmt = (
				select(Batch)
				.where(
					Batch.product_id == line.product_id,
					Batch.warehouse_id == doc.warehouse_id,
					Batch.current_quantity > 0
				)
				.order_by(Batch.created_at.asc()) # FIFO: сначала более ранние партии
			)
			batches = self._session.execute(stmt).scalars().all()

			# Проверка остатка перед списанием
			total_available = sum(b.current_quantity for b in batches)
			if total_available < qty_to_ship:
				raise InsufficientStockError(
				line.product_id, line.product.name,
				doc.warehouse_id, doc.warehouse.name,
				line.quantity, total_available
			)

			# 3. Списание по партиям
			first_batch = True
			cost_sum = 0
			for batch in batches:
				if qty_to_ship <= 0:
					break

				can_take = min(batch.current_quantity, qty_to_ship)

				if first_batch:
					# Для первой партии используем уже существующую строку документа
					line.quantity = can_take
					line.applied_batch_id = batch.id  # Сохраняем связь для отчета!
					line.cost_price = batch.purchase_price # себестоимость
					first_batch = False
				else:
					# Если товар берется из второй и далее партии, создаем НОВУЮ строку
					new_line = DocumentLine(
						document_id=doc.id,
						product_id=line.product_id,
						quantity=can_take,
						price=line.price, # Продажная цена остается как в оригинале
						cost_price=batch.purchase_price, # Себестоимость из этой партии
						applied_batch_id=batch.id # Привязка к партии
					)
					self._session.add(new_line)
				cost_sum += batch.purchase_price

				# Физически уменьшаем остаток в регистре (Batch)
				batch.current_quantity -= can_take
				qty_to_ship -= can_take

			# Запись в регистр оборотов
			entry = SalesAccumulator(
				date=doc.date,
				document_id=doc.id,
				product_id=line.product_id,
				counterparty_id=doc.counterparty_id,
				warehouse_id=doc.warehouse_id,
				quantity=line.quantity,
				sale_price=line.price,
				total_sale_sum=line.quantity * line.price,
				total_cost_sum=cost_sum
			)
			self._session.add(entry)

		doc.is_posted = True
		self._session.commit()

	def post_order(self, document_id: int):
		"""
		Провести замовлення покупця.
		Тільки змінює статус проведеності.
		"""
		doc = self._session.get(Document, document_id)
		self._check_not_posted(doc) # Проверка, что документ еще не проведен

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
		if not doc:
			raise ValueError("Документ не знайдено")

		if not doc.is_posted:
			return doc

		# Удаляем записи из регистра продаж по этому документу
		self._session.execute(
			delete(SalesAccumulator).where(SalesAccumulator.document_id == document_id)
		)

		# Возвращаем остатки в партии
		for line in doc.lines:
			if line.applied_batch_id:
				batch = self._session.get(Batch, line.applied_batch_id)
				if batch:
					batch.current_quantity += line.quantity

				# Очищаем связи в строке
				line.applied_batch_id = None
				line.cost_price = None

		doc.is_posted = False
		self._session.flush()

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

	def create_invoice_from_order(self, order_id: int) -> Document:
		doc = self._create_from_parent(order_id, DocumentType.INVOICE)
		self._session.commit()
		return doc

	def create_outgoing_from_invoice(self, invoice_id: int) -> Document:
		doc = self._create_from_parent(invoice_id, DocumentType.OUT)
		self._session.commit()
		return doc

	def create_tax_from_outgoing(self, invoice_id: int) -> Document:
		doc = self._create_from_parent(invoice_id, DocumentType.TAX)
		self._session.commit()
		return doc

	def _check_not_posted(self, doc: Document) -> None:
		"""Запретить перепроведение."""
		if doc.is_posted:
			raise DocumentAlreadyPostedError(
				f"Документ №{doc.number} (id={doc.id}) уже проведён. "
				"Перепроведение запрещено. Сначала отмените документ."
			)

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