from __future__ import annotations # для использования классов в аннотациях типов до их определения
from datetime import date, datetime
from decimal import Decimal
import enum

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import (Mapped, mapped_column, relationship)
from sqlalchemy import (
	Boolean, Date, DateTime, Enum as SAEnum, ForeignKey,
	Integer, Numeric, String, Text
)


db = SQLAlchemy()

class DocumentType(str, enum.Enum):
	ORDER = "ORDER"
	INVOICE = "INVOICE"
	IN = "IN"
	OUT = "OUT"
	TAX = "TAX"

	@property
	def label(self):
		labels = {
			'IN': 'Прихід',
			'OUT': 'Видаток',
			'TAX': 'Податкова',
			'ORDER': 'Замовлення',
			'INVOICE': 'Рахунок-фактура'
		}
		return labels.get(self.value, self.value)

class Counterparty(db.Model):
	__tablename__ = "counterparties" # Имя таблицы БД
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(200), nullable=False)
	is_customer: Mapped[bool] = mapped_column(Boolean, default=False)
	is_supplier: Mapped[bool] = mapped_column(Boolean, default=False)
	documents: Mapped[list[Document]] = relationship(back_populates="counterparty")

class Warehouse(db.Model):
	__tablename__ = "warehouses"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(100), nullable=False)

	documents: Mapped[list[Document]] = relationship(back_populates="warehouse")
	batches: Mapped[list[Batch]] = relationship(back_populates="warehouse")

class Product(db.Model):
	"""
	Справочник товаров и услуг.

	Если is_service=True — позиция является услугой:
	партии Batch для неё не создаются, FIFO не применяется
	"""

	__tablename__ = "products"
	id: Mapped[int] = mapped_column(primary_key=True)
	name: Mapped[str] = mapped_column(String(200), nullable=False)
	unit: Mapped[str] = mapped_column(String(50), nullable=False, default="шт",
		comment="Единица измерения (шт, кг, л и т.д.)"
	)
	is_service: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
		comment="True — услуга, FIFO не применяется"
	)

	document_lines: Mapped[list[DocumentLine]] = relationship(back_populates="product")
	batches: Mapped[list[Batch]] = relationship(back_populates="product",
		order_by="Batch.created_at"	 # удобно для FIFO-выборки
	)

	def __repr__(self) -> str:
		kind = "Услуга" if self.is_service else "Товар"
		return (
			f"<Product id={self.id} unit={self.unit!r} "
			f"name={self.name!r} [{kind}]>"
		)

class Document(db.Model):
	"""
	Заголовок накладной.

	doc_type=IN	 — приход: создаёт партии Batch.
	doc_type=OUT — расход: списывает партии по FIFO
	"""

	__tablename__ = "documents"
	id: Mapped[int] = mapped_column(
		Integer,
		primary_key=True
	)
	is_posted: Mapped[bool] = mapped_column(
		Boolean,
		nullable=False,
		default=False,
		comment="Признак проведённого документа"
	)
	number: Mapped[str] = mapped_column(
		String(100),
		nullable=False,
		unique=True,
		comment="Номер накладной"
	)
	doc_type: Mapped[DocumentType] = mapped_column(
		SAEnum(DocumentType),
		nullable=False,
		comment="IN — приходная, OUT — расходная"
	)
	date: Mapped[datetime] = mapped_column(
		DateTime,
		nullable=False,
		default=datetime.now
	)
	note: Mapped[str | None] = mapped_column(Text)

	# Документ-основание
	parent_id: Mapped[int | None] = mapped_column(
		db.ForeignKey("documents.id"),
		nullable=True
	)
	parent: Mapped["Document | None"] = relationship("Document", remote_side="Document.id")

	# Склад
	warehouse_id: Mapped[int | None] = mapped_column(
		db.ForeignKey('warehouses.id'))
	warehouse: Mapped[Warehouse] = relationship(back_populates="documents")

	# Контрагент
	counterparty_id: Mapped[int | None] = mapped_column(
		db.ForeignKey('counterparties.id')
	)
	counterparty: Mapped[Counterparty] = relationship(back_populates="documents")

	# Табличная часть
	lines: Mapped[list[DocumentLine]] = relationship(back_populates="document",
		cascade="all, delete-orphan"
	)

	def __repr__(self) -> str:
		return (
			f"<Document id={self.id} number={self.number!r} "
			f"type={self.doc_type.value} date={self.date}>"
		)

class DocumentLine(db.Model):
	"""
	Строка накладной: товар, количество, цена.

	Для документа типа IN каждая строка является источником одной партии Batch
	(связь 1:1 через Batch.incoming_line_id)
	"""

	__tablename__ = "document_lines"
	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	document_id: Mapped[int] = mapped_column(
		ForeignKey("documents.id"), nullable=False
	)
	product_id: Mapped[int] = mapped_column(
		ForeignKey("products.id"), nullable=False
	)
	quantity: Mapped[float] = mapped_column(
		Numeric(14, 4), nullable=False,
		comment="Количество"
	)
	price: Mapped[float] = mapped_column(
		Numeric(14, 4), nullable=False,
		comment="Цена за единицу"
	)
	cost_price: Mapped[float | None] = mapped_column(
		Numeric(14, 4), nullable=True,
		comment="Себестоимость списания (заполняется при проведении OUT)"
)
	document: Mapped[Document] = relationship(back_populates="lines")
	product: Mapped[Product] = relationship(back_populates="document_lines")
	# Приход: связь строки прихода с созданной ею партией
	batch: Mapped["Batch | None"] = relationship(
		"Batch",
		back_populates="incoming_line", uselist=False,
		# Если строка документа создается/удаляется - создать/удалить партию
		cascade="all, delete-orphan",
		foreign_keys="Batch.incoming_line_id"
	)
	# Расход: связь указывает, какую партию списала эта строка
	applied_batch_id: Mapped[int | None] = mapped_column(
		ForeignKey("batches.id",
		use_alter=True,
		name="fk_applied_batch")
	)
	applied_batch: Mapped["Batch | None"] = relationship("Batch",
		foreign_keys=[applied_batch_id]
	)

	@property
	def total(self) -> float:
		"""Сумма строки (quantity × price)."""
		return float(self.quantity) * float(self.price)

	def __repr__(self) -> str:
		return (
			f"<DocumentLine id={self.id} doc_id={self.document_id} "
			f"product_id={self.product_id} "
			f"qty={self.quantity} price={self.price}>"
		)

# Регистры

class Batch(db.Model):
	"""
	Партия товара: создаётся при проведении Приходной накладной.
	Сортировка по created_at ASC для списания по FIFO.

	Поля:
		initial_quantity  — исходное количество (не меняется, для истории).
		current_quantity  — остаток, доступный для списания; уменьшается
							сервисным слоем при проведении Расходных накладных
	"""

	__tablename__ = "batches"
	id: Mapped[int] = mapped_column(Integer, primary_key=True)
	product_id: Mapped[int] = mapped_column(
		ForeignKey("products.id"), nullable=False, index=True
	)
	incoming_line_id: Mapped[int | None] = mapped_column(
		ForeignKey("document_lines.id"),
		nullable=False, unique=True # одна строка прихода - одна партия
	)
	purchase_price: Mapped[float] = mapped_column(
		Numeric(14, 4), nullable=False,
		comment="Цена закупки (из строки прихода)"
	)
	initial_quantity: Mapped[float] = mapped_column(
		Numeric(14, 4), nullable=False,
		comment="Принятое количество (неизменно)"
	)
	current_quantity: Mapped[float] = mapped_column(
		Numeric(14, 4), nullable=False,
		comment="Остаток для FIFO-списания"
	)
	created_at: Mapped[datetime] = mapped_column(
		DateTime, nullable=False, index=True, default=datetime.utcnow,
		comment="Дата/время создания — определяет порядок FIFO"
	)
	warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))

	warehouse: Mapped[Warehouse] = relationship(back_populates="batches")
	product: Mapped[Product] = relationship(back_populates="batches")
	incoming_line: Mapped[DocumentLine | None] = relationship(
		"DocumentLine",
		back_populates="batch",
		foreign_keys=[incoming_line_id]
	)

	@property
	def is_exhausted(self) -> bool:
		"""True, если партия полностью списана."""
		return float(self.current_quantity) <= 0

	def __repr__(self) -> str: return (
		f"<Batch id={self.id} product_id={self.product_id} "
		f"price={self.purchase_price} "
		f"qty={self.current_quantity}/{self.initial_quantity} "
		f"created={self.created_at.date()}>"
	)

class SalesAccumulator(db.Model):
	__tablename__ = 'sales_accumulator'

	id: Mapped[int] = mapped_column(primary_key=True)
	date: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
	document_id: Mapped[int] = mapped_column(ForeignKey('documents.id'), nullable=False)
	product_id: Mapped[int] = mapped_column(ForeignKey('products.id'), nullable=False)
	counterparty_id: Mapped[int] = mapped_column(ForeignKey('counterparties.id'), nullable=False)
	warehouse_id: Mapped[int] = mapped_column(ForeignKey('warehouses.id'), nullable=False)

	quantity: Mapped[Decimal] = mapped_column(Numeric(15, 4), default=0)
	sale_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
	total_sale_sum: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)
	total_cost_sum: Mapped[Decimal] = mapped_column(Numeric(15, 2), default=0)

	# Отношения (Relationship)
	product: Mapped["Product"] = relationship()
	counterparty: Mapped["Counterparty"] = relationship()
	warehouse: Mapped["Warehouse"] = relationship()
	document: Mapped["Document"] = relationship()