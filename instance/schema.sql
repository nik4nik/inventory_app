CREATE TABLE counterparties (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	is_customer BOOLEAN NOT NULL, 
	is_supplier BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE warehouses (
	id INTEGER NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE products (
	id INTEGER NOT NULL, 
	name VARCHAR(200) NOT NULL, 
	unit VARCHAR(50) NOT NULL, 
	is_service BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);
CREATE TABLE documents (
	id INTEGER NOT NULL, 
	is_posted BOOLEAN NOT NULL, 
	number VARCHAR(100) NOT NULL, 
	doc_type VARCHAR(7) NOT NULL, 
	date DATE NOT NULL, 
	note TEXT, 
	parent_id INTEGER, 
	warehouse_id INTEGER, 
	counterparty_id INTEGER, 
	PRIMARY KEY (id), 
	UNIQUE (number), 
	FOREIGN KEY(parent_id) REFERENCES documents (id), 
	FOREIGN KEY(warehouse_id) REFERENCES warehouses (id), 
	FOREIGN KEY(counterparty_id) REFERENCES counterparties (id)
);
CREATE TABLE document_lines (
	id INTEGER NOT NULL, 
	document_id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	quantity NUMERIC(14, 4) NOT NULL, 
	price NUMERIC(14, 4) NOT NULL, 
	cost_price NUMERIC(14, 4), 
	applied_batch_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(product_id) REFERENCES products (id), 
	CONSTRAINT fk_applied_batch FOREIGN KEY(applied_batch_id) REFERENCES batches (id)
);
CREATE TABLE sales_accumulator (
	id INTEGER NOT NULL, 
	date DATE NOT NULL, 
	document_id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	counterparty_id INTEGER NOT NULL, 
	warehouse_id INTEGER NOT NULL, 
	quantity NUMERIC(15, 4) NOT NULL, 
	sale_price NUMERIC(15, 2) NOT NULL, 
	total_sale_sum NUMERIC(15, 2) NOT NULL, 
	total_cost_sum NUMERIC(15, 2) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(document_id) REFERENCES documents (id), 
	FOREIGN KEY(product_id) REFERENCES products (id), 
	FOREIGN KEY(counterparty_id) REFERENCES counterparties (id), 
	FOREIGN KEY(warehouse_id) REFERENCES warehouses (id)
);
CREATE INDEX ix_sales_accumulator_date ON sales_accumulator (date);
CREATE TABLE batches (
	id INTEGER NOT NULL, 
	product_id INTEGER NOT NULL, 
	incoming_line_id INTEGER NOT NULL, 
	purchase_price NUMERIC(14, 4) NOT NULL, 
	initial_quantity NUMERIC(14, 4) NOT NULL, 
	current_quantity NUMERIC(14, 4) NOT NULL, 
	created_at DATETIME NOT NULL, 
	warehouse_id INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(product_id) REFERENCES products (id), 
	UNIQUE (incoming_line_id), 
	FOREIGN KEY(incoming_line_id) REFERENCES document_lines (id), 
	FOREIGN KEY(warehouse_id) REFERENCES warehouses (id)
);
CREATE INDEX ix_batches_created_at ON batches (created_at);
CREATE INDEX ix_batches_product_id ON batches (product_id);
