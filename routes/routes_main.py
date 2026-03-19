from datetime import date, datetime, timedelta

from flask import Blueprint, render_template, request
from sqlalchemy import and_, func, select

from models import (Batch, db, Document, DocumentLine, DocumentType, Warehouse,
	Product, SalesAccumulator)


main_bp = Blueprint('main', __name__)

@main_bp.route('/')
def index():
	return render_template('index.html')

@main_bp.route('/report_remainings')
def report_remainings():
	date_str = request.args.get('date')
	target_date = (datetime.strptime(date_str, '%Y-%m-%d').date() if date_str
				  else datetime.now().date())
	warehouse_id = request.args.get('warehouse_id', type=int)

	# общий расход каждого товара на каждом складе
	out_subquery = (
		select(
			DocumentLine.product_id,
			Document.warehouse_id,
			func.sum(DocumentLine.quantity).label('total_out')
		)
		.join(Document, DocumentLine.document_id == Document.id)
		.where(and_(
			Document.date <= target_date,
			Document.is_posted == True,
			Document.doc_type == DocumentType.OUT
		))
		.group_by(DocumentLine.product_id, Document.warehouse_id)
	).subquery()

	# Все приходы сопоставляем с общим расходом
	stmt = (
		select(
			Warehouse.name.label('wh_name'),
			Product.name.label('prod_name'),
			Batch.purchase_price,
			DocumentLine.quantity.label('in_qty'),
			func.coalesce(out_subquery.c.total_out, 0).label('total_out_for_product'),
			Batch.id.label('batch_id')
		)
		.join(Batch, Batch.incoming_line_id == DocumentLine.id)
		.join(Document, DocumentLine.document_id == Document.id)
		.join(Product, Batch.product_id == Product.id)
		.join(Warehouse, Batch.warehouse_id == Warehouse.id)
		.outerjoin(out_subquery, and_(
			Batch.product_id == out_subquery.c.product_id,
			Batch.warehouse_id == out_subquery.c.warehouse_id
		))
		.where(and_(
			Document.date <= target_date,
			Document.is_posted == True
		))
		.order_by(Warehouse.name, Product.name, Document.date) # FIFO
	)

	if warehouse_id:
		stmt = stmt.where(Warehouse.id == warehouse_id)

	results = db.session.execute(stmt).all()
	report_data = {}
	grand_total = 0

	# словарь для распределения общего расхода по партиям
	consumption_tracker = {}

	for row in results:
		key = (row.wh_name, row.prod_name)
		if key not in consumption_tracker:
			consumption_tracker[key] = row.total_out_for_product

		# Сколько из этой партии уже израсходовано
		can_consume = min(row.in_qty, consumption_tracker[key])
		balance = row.in_qty - can_consume
		consumption_tracker[key] -= can_consume

		if balance <= 0: continue

		cost_sum = balance * row.purchase_price

		if row.wh_name not in report_data:
			report_data[row.wh_name] = {'products': {}, 'wh_total_sum': 0}

		if row.prod_name not in report_data[row.wh_name]['products']:
			report_data[row.wh_name]['products'][row.prod_name] = {'qty': 0, 'sum': 0}

		report_data[row.wh_name]['products'][row.prod_name]['qty'] += balance
		report_data[row.wh_name]['products'][row.prod_name]['sum'] += cost_sum
		report_data[row.wh_name]['wh_total_sum'] += cost_sum
		grand_total += cost_sum

	warehouses = db.session.execute(select(Warehouse)).scalars().all()

	return render_template('reports.html',
							report_data=report_data,
							grand_total=grand_total,
							warehouses=warehouses,
							selected_warehouse=warehouse_id,
							selected_date=target_date.strftime('%Y-%m-%d'))

@main_bp.route('/report_sales')
def report_sales():
	start_date_str = request.args.get('start_date')
	end_date_str = request.args.get('end_date')

	# текущий месяц
	today = datetime.now().date()
	start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else today.replace(day=1)
	end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else today

	stmt = (
		select(
			Product.name.label('product_name'),
			func.sum(SalesAccumulator.quantity).label('total_qty'),
			func.sum(SalesAccumulator.total_sale_sum).label('total_sales'),
			func.sum(SalesAccumulator.total_cost_sum).label('total_costs')
		)
		.join(Product, SalesAccumulator.product_id == Product.id)
		.where(SalesAccumulator.date.between(start_date, end_date))
		.group_by(Product.name)
	)

	results = db.session.execute(stmt).all()

	sales_data = []
	total_profit = 0

	for row in results:
		profit = row.total_sales - row.total_costs
		profit_margin = (profit / row.total_sales * 100) if row.total_sales != 0 else 0

		sales_data.append({
			'product': row.product_name,
			'qty': row.total_qty,
			'sales_sum': row.total_sales,
			'cost_sum': row.total_costs,
			'profit': profit,
			'margin': profit_margin
		})
		total_profit += profit

	return render_template('report_sales.html',
						   data=sales_data,
						   total_profit=total_profit,
						   start_date=start_date,
						   end_date=end_date)

@main_bp.route('/report_income')
def report_income():
	# по умолчанию за 30 дней
	start_str = request.args.get('start_date')
	end_str = request.args.get('end_date')

	end_date = datetime.strptime(end_str, '%Y-%m-%d').date() if end_str else date.today()
	start_date = datetime.strptime(start_str, '%Y-%m-%d').date() if start_str else (end_date - timedelta(days=30))

	# Запрос к регистру оборотов
	stmt = (
		select(
			Product.name.label('product_name'),
			func.sum(SalesAccumulator.quantity).label('qty'),
			func.sum(SalesAccumulator.total_sale_sum).label('sales_sum'),
			func.sum(SalesAccumulator.total_cost_sum).label('cost_sum')
		)
		.join(Product, SalesAccumulator.product_id == Product.id)
		.where(SalesAccumulator.date.between(start_date, end_date))
		.group_by(Product.name)
		.order_by(func.sum(SalesAccumulator.total_sale_sum - SalesAccumulator.total_cost_sum).desc())
	)

	results = db.session.execute(stmt).all()

	report_rows = []
	grand_sales = 0
	grand_cost = 0

	for row in results:
		profit = row.sales_sum - row.cost_sum
		margin = (profit / row.sales_sum * 100) if row.sales_sum > 0 else 0

		report_rows.append({
			'name': row.product_name,
			'qty': row.qty,
			'sales': row.sales_sum,
			'cost': row.cost_sum,
			'profit': profit,
			'margin': margin
		})

		grand_sales += row.sales_sum
		grand_cost += row.cost_sum

	grand_profit = grand_sales - grand_cost

	return render_template('report_income.html',
							rows=report_rows,
							start_date=start_date.strftime('%Y-%m-%d'),
							end_date=end_date.strftime('%Y-%m-%d'),
							grand_sales=grand_sales,
							grand_cost=grand_cost,
							grand_profit=grand_profit)