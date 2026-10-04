# Local Postgres 17 + pgvector 0.8.0 (the versions Neon runs), for development and evals.
# Built on plain Alpine with Alpine's own Postgres packages.
FROM alpine:3
ARG PGVECTOR_VERSION=0.8.0
ARG PG_BIN=/usr/libexec/postgresql17
# Alpine's default pg_config can point at a newer major version; pin everything to 17.
ENV PATH=${PG_BIN}:$PATH
RUN apk add --no-cache postgresql17 postgresql17-contrib \
 && apk add --no-cache --virtual .build-deps git build-base postgresql17-dev \
 && git clone --depth 1 --branch v${PGVECTOR_VERSION} https://github.com/pgvector/pgvector.git /tmp/pgvector \
 && make -C /tmp/pgvector with_llvm=no PG_CONFIG=${PG_BIN}/pg_config \
 && make -C /tmp/pgvector install with_llvm=no PG_CONFIG=${PG_BIN}/pg_config \
 && rm -rf /tmp/pgvector \
 && apk del .build-deps \
 && mkdir -p /run/postgresql /var/lib/postgresql/data \
 && chown -R postgres:postgres /run/postgresql /var/lib/postgresql
USER postgres
ENV PGDATA=/var/lib/postgresql/data
# Throwaway local database: trust auth on purpose (never use this image anywhere else).
RUN initdb -U postgres --auth=trust \
 && echo "listen_addresses='*'" >> $PGDATA/postgresql.conf \
 && echo "host all all 0.0.0.0/0 trust" >> $PGDATA/pg_hba.conf
EXPOSE 5432
CMD ["postgres"]
