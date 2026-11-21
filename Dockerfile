FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ENV PYTHONDONTWRITEBYTECODE=1
ADD https://codeload.github.com/tobymao/sqlglot/zip/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.zip
COPY tools/extract_sqlglot.py tools/verify_tree.py /recipe/
RUN python /recipe/extract_sqlglot.py /tmp/sqlglot.zip /baseline \
    && python /recipe/verify_tree.py /baseline 6606fc5229c882bd0cf8f9ba79503e9e10d084f7 \
    && rm /tmp/sqlglot.zip

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runner
WORKDIR /app
COPY --from=source /baseline/ /app/base/
COPY src/ /app/umbermill/
COPY maps/ /app/maps/
RUN echo 'e66001a1db1b75a374aa909b4a90ef2c3a469bb96af09c4ca03c58693653d133  /app/maps/conditions.json.gz' | sha256sum -c - && echo 'fe6a90915549597b680d95e04ac5a2b06965b1178b7f9cdde96828cb400574b7  /app/maps/fields.json.gz' | sha256sum -c - && echo '410a6318b64e6512dc06ed259b7c77cd4d63e88b226156de91e43dd43799f291  /app/maps/impact.json.gz' | sha256sum -c - && echo '53dd0a46a3886a95100a4f3cf30eb2227281cf992203574363af90edc1c080c4  /app/maps/isolated.json.gz' | sha256sum -c - && echo '4c3bd48e7d1b430935bf36368db9e8bfec13736c9430e38045f8fd069199fd33  /app/maps/readers.json.gz' | sha256sum -c - && python -m compileall -q --invalidation-mode checked-hash /app/umbermill
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IMPACT_GRANULARITY=function
USER 65534:65534
ENTRYPOINT ["python", "-m", "umbermill"]
