from datetime import date

from sqlalchemy import select

from app import create_app
from models import db, Document, DocumentLine, Batch, DocumentType, Warehouse, Product, Counterparty
from inventory_service import InventoryService

app = create_app()

with app.app_context():
	db.drop_all()
	db.create_all()
	service = InventoryService(db.session)

	warehouses = [
		Warehouse(name="Центральний склад"),
		Warehouse(name="Західний філіал"),
		Warehouse(name="Склад готової продукції"),
		Warehouse(name="Транзитний склад")
	]
	db.session.add_all(warehouses)

	supplier = Counterparty(name="ТОВ 'Постачальник Плюс'", is_supplier=True)
	customer = Counterparty(name="ПП 'Роздрібний Клієнт'", is_customer=True)
	db.session.add_all([supplier, customer])

	products = [
		Product(name="Цегла червона", unit="шт"),
		Product(name="Цемент М-500", unit="міш"),
		Product(name="Пісок річковий", unit="т"),
		Product(name="Арматура 12мм", unit="м"),
		Product(name="Бетонозмішувач", unit="шт"),
		Product(name="Фарба фасадна", unit="відро"),
		Product(name="Гіпсокартон", unit="лист"),
		Product(name="Клей для плитки", unit="міш"),
		Product(name="Шпаклівка", unit="міш"),
		Product(name="Грунтовка", unit="каністра")
	]
	db.session.add_all(products)
	db.session.commit()

	# Приходы на склады
	for i, wh in enumerate(warehouses):
		doc_type = DocumentType.IN
		doc_in = service.create_document(
			doc_type = DocumentType.IN,
			date = date(2026, 3, 1),
			warehouse_id = wh.id,
			counterparty_id = supplier.id
		)
		db.session.add(doc_in)
		db.session.flush()

		for j in range(2):
			prod_idx = (i * 2 + j) % len(products)
			line = DocumentLine(
				document_id=doc_in.id,
				product_id=products[prod_idx].id,
				quantity=50.0,
				price=100.0 + (prod_idx * 10)
			)
			db.session.add(line)

		db.session.commit()
		# Проводим, чтобы появились партии (Batch)
		service.post_incoming_invoice(doc_in.id)

	# После всех приходов сбрасываем состояние объектов в памяти
	db.session.expire_all()
	print(f"✔ Створено 4 склади та 10 товарів. Склади наповнені початковими партіями.")

	# --- Подготовка к тесту FIFO ---
	target_product = products[0] # Цегла червона (ID=1)
	target_wh = warehouses[0] # Центральний склад (ID=1)

	# Создаем ВТОРУЮ партию того же товара (дороже)
	doc_in_extra = Document(
		number="ПН-EXTRA", date=date(2026, 3, 2), doc_type=DocumentType.IN,
		warehouse_id=target_wh.id, counterparty_id=supplier.id
	)
	db.session.add(doc_in_extra)
	db.session.flush()

	line_extra = DocumentLine(
		document_id=doc_in_extra.id,
		product_id=target_product.id,
		quantity=10.0,
		price=150.0
	)
	db.session.add(line_extra)
	db.session.commit()
	service.post_incoming_invoice(doc_in_extra.id)
	db.session.expire_all()

	# --- Тестовый расход ---
	doc_out = Document(
		number="РН-ТЕСТ", date=date(2026, 3, 7), doc_type=DocumentType.OUT,
		warehouse_id=target_wh.id, counterparty_id=customer.id
	)
	db.session.add(doc_out)
	db.session.flush()

	line_out = DocumentLine(
		document_id=doc_out.id,
		product_id=target_product.id,
		quantity=55.0,
		price=200.0
	)
	db.session.add(line_out)
	db.session.commit()

	print(f"--- Тест: Видаткова накладна на 55 од. '{target_product.name}' ---")

	# Теперь партии есть в базе (is_posted=True у приходов)
	service.post_outgoing_invoice(doc_out.id)

	# Проверка расщепления (FIFO)
	db.session.expire_all() # Принудительно обновляем данные из БД
	res_lines = db.session.execute(
		select(DocumentLine).where(DocumentLine.document_id == doc_out.id)
	).scalars().all()

	print(f"Кількість рядків після розщеплення: {len(res_lines)}")
	for l in res_lines:
		print(f"  -> К-сть: {l.quantity}, Собівартість: {l.cost_price}, Партія ID: {l.applied_batch_id}")

	# Тест отмены
	print("\n--- Тест скасування проведення ---")
	service.unpost_outgoing_invoice(doc_out.id)
	db.session.commit()

	# Проверка возврата: находим строку из ПН-001 для "Цегла"
	# Это была первая строка первого документа в цикле.
	first_doc_line = db.session.scalar(
		select(DocumentLine)
		.join(Document)
		.where(Document.number == "ПН-001")
		.where(DocumentLine.product_id == target_product.id)
	)
	# Ищем партию привязанную к этой строке
	batch1 = db.session.scalar(
		select(Batch).where(Batch.incoming_line_id == first_doc_line.id)
	)
	# После того как service.unpost_outgoing_invoice выполнил commit,
	# объект batch1 может всё еще хранить старое (списанное) значение.
	db.session.expire_all()

	print(f"Залишок у партії №1 після скасування: {batch1.current_quantity} (Очікувано 50.0)" if batch1
		else "Ошибка: Партия №1 не найдена!")