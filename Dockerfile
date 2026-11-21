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
RUN echo 'ca41569f4fafb8cd0a539e8d8177ea58916333d8b2ea833738ac89e05dd2a552  /app/maps/impact.json.gz' | sha256sum -c - && echo '122b3710ad9ec82a852ecbe1301d38d0aa896dd0ab40aab5fd5fd439723081d9  /app/maps/fields.json.gz' | sha256sum -c - && echo '886ca66ac9b0aaea516bb33ed23f111bea1b0cb7f14fa62bab197cf6b6231659  /app/maps/readers.json.gz' | sha256sum -c - && python -m compileall -q --invalidation-mode checked-hash /app/umbermill
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IMPACT_GRANULARITY=function
USER 65534:65534
ENTRYPOINT ["python", "-m", "umbermill"]
