from flask import Flask

from models import db # экземпляр класса SQLAlchemy
from routes.routes_main import main_bp
from routes.routes_docs import docs_bp


def create_app():
	app = Flask(__name__)
	# Объект config передает настройки расширениям (SQLAlchemy)
	app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trade_company.db'
	# Отключим отслеживание изменений объекта SQLAlchemy
	app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
	app.config['SECRET_KEY'] = 'XXX'
	app.register_blueprint(main_bp)
	app.register_blueprint(docs_bp)

	db.init_app(app)
	with app.app_context():
		db.create_all()

	return app

if __name__ == '__main__':
	app = create_app()
	app.run(debug=True) # автоперезагрузка