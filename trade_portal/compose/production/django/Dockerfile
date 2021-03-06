FROM python:3.7-slim-buster

ENV PYTHONUNBUFFERED 1

RUN apt-get update \
  # dependencies for building Python packages
  && apt-get install -y build-essential git \
  # psycopg2 dependencies
  && apt-get install -y libpq-dev \
  # Translations dependencies
  && apt-get install -y gettext \
  # Pdf/Image deps
  && apt-get install -y libmagickwand-dev ghostscript libgl-dev libpoppler-cil poppler-utils tesseract-ocr libzbar0 \
  # cleaning up unused files
  && apt-get purge -y --auto-remove -o APT::AutoRemove::RecommendsImportant=false \
  && rm -rf /var/lib/apt/lists/*

RUN addgroup --system django \
  && adduser --system --ingroup django django

# Requirements are installed here to ensure they will be cached.
COPY ./requirements /requirements
RUN pip install --no-cache-dir -r /requirements/production.txt \
  && rm -rf /requirements

COPY ./compose/production/django/entrypoint /entrypoint
RUN sed -i 's/\r$//g' /entrypoint
RUN chmod +x /entrypoint
RUN chown django /entrypoint

COPY ./compose/production/django/start /start
RUN sed -i 's/\r$//g' /start && \
    chmod +x /start && \
    chown django /start

COPY ./compose/production/django/celery/worker/start /start-celeryworker
RUN sed -i 's/\r$//g' /start-celeryworker && \
    chmod +x /start-celeryworker && \
    chown django /start-celeryworker

COPY ./compose/production/django/celery/beat/start /start-celerybeat
RUN sed -i 's/\r$//g' /start-celerybeat && \
    chmod +x /start-celerybeat && \
    chown django /start-celerybeat

COPY ./compose/production/django/celery/flower/start /start-flower
RUN sed -i 's/\r$//g' /start-flower && \
    chmod +x /start-flower

COPY --chown=django:django . /app

RUN bash -c "set -o allexport && source /app/devops/localdocker/common.env && DJANGO_SETTINGS_MODULE="config.settings.production" DJANGO_SECRET_KEY="123" DATABASE_URL="postgres://" python /app/manage.py collectstatic --noinput" > /dev/null
RUN bash -c "set -o allexport && source /app/devops/localdocker/common.env && DJANGO_SETTINGS_MODULE="config.settings.production" DJANGO_SECRET_KEY="123" DATABASE_URL="postgres://" python /app/manage.py compilemessages" > /dev/null

# enable PDF conversion in wand/imagemagick
RUN python -c "result = open('/etc/ImageMagick-6/policy.xml').read().replace('rights="\""none"\"" pattern="\""PDF"\""', 'rights="\""read | write"\"" pattern="\""PDF"\""'); open('/etc/ImageMagick-6/policy.xml', 'w').write(result)"

RUN chmod -R 777 /tmp

USER django

WORKDIR /app

ENTRYPOINT ["/entrypoint"]
