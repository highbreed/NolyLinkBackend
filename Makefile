.PHONY: dev test migrate lint fmt typecheck down logs

dev:
	docker-compose up --build

down:
	docker-compose down

logs:
	docker-compose logs -f app

test:
	docker-compose run --rm app pytest

migrate:
	docker-compose run --rm app alembic upgrade head

revision:
	docker-compose run --rm app alembic revision --autogenerate -m "$(m)"

lint:
	docker-compose run --rm app ruff check app tests

fmt:
	docker-compose run --rm app black app tests

typecheck:
	docker-compose run --rm app mypy app
