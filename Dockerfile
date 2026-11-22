FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS source
ENV PYTHONDONTWRITEBYTECODE=1
ADD https://codeload.github.com/tobymao/sqlglot/zip/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.zip
COPY tools/extract_sqlglot.py tools/verify_tree.py /recipe/
RUN python /recipe/extract_sqlglot.py /tmp/sqlglot.zip /baseline \
    && python /recipe/verify_tree.py /baseline 6606fc5229c882bd0cf8f9ba79503e9e10d084f7 \
    && rm /tmp/sqlglot.zip

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS runner
WORKDIR /app
ARG INTERPRETER_BUILD=auto
COPY tools/select_python.py tools/sqlite_site.py /recipe/
RUN timeout 55 /usr/local/bin/python /recipe/select_python.py --preference "$INTERPRETER_BUILD" \
    || /usr/local/bin/python /recipe/select_python.py --preference base
RUN find /opt/umbermill \( -type d ! -perm -005 -o -type f ! -perm -004 \) -exec chmod a+rX {} +
ENV PATH=/opt/umbermill/bin:$PATH OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 MIMALLOC_LARGE_OS_PAGES=1 PYTHONHASHSEED=0
COPY requirements.lock /recipe/requirements.lock
RUN python -m pip install --disable-pip-version-check --no-cache-dir --no-deps -r /recipe/requirements.lock && python -m pip check
COPY --from=source /baseline/ /app/base/
COPY src/ /app/umbermill/
COPY maps/ /app/maps/
RUN echo '07bdafc26ac3a3c07ce9a549506d6549019576a7c3f827132091c5e8cd070e62  /app/maps/class-declarations.json.gz' | sha256sum -c -
RUN echo '2b0e84b14b71887323e2a46f17e0f79e18fa63f0486ea074bedf25c5bbc7bdc5  /app/maps/effective-dispatch.json.gz' | sha256sum -c -
RUN echo '5df3235e6e4244c292617672c7ce92e7ad212421fb87d3cfd732a45672582fff  /app/maps/paired-operands.json.gz' | sha256sum -c -
RUN echo 'c42d8029c9839654c432ec29e1a1c8121d21ffa92452c380f643f622ca0d7f30  /app/maps/fresh-process.json.gz' | sha256sum -c -
RUN echo '7f2e68ae9c93e88542ea0bac3642f2c553d468807a0a5838a779b26eafa20402  /app/maps/operand-observations.json.gz' | sha256sum -c -
RUN echo '007a8ad363af7aaf12b91d1743e1a146bcbd13baa5a8edb09871e672bead7866  /app/maps/passive-kernel.json.gz' | sha256sum -c -
RUN echo 'c6f205103d2ef8f1f3da1dec1c7b157235c88bd4399b315f074363cc914c23cf  /app/maps/path-observations.json.gz' | sha256sum -c -
RUN echo 'b929c32d109088d4db98f4ee20217b742636ae70b0b212ff1040335dc3e52e95  /app/maps/module-cold.json.gz' | sha256sum -c -
RUN echo 'a5d10601a1d89c48b9825bcba521b62583672ac9608be033dc99ab1207a6292a  /app/maps/primitives.json.gz' | sha256sum -c -
RUN echo '887290cfd542edaa0ced21487cf0a6a038db0ca143b866377927c46fbe2848b4  /app/maps/executor-phases.json.gz' | sha256sum -c -
COPY tools/prepare_index.py tools/prepare_code.py tools/prepare_maps.py tools/prepare_docs.py /recipe/
RUN echo 'e0a41e0e8f80a763e449125d18f031dcdbd4819c0ade828a9033892ca02c3dbf  /app/maps/conditions.json.gz' | sha256sum -c - && echo 'c7e6e22f0baeb681a48041ea7a2d213e242b6a6180ff7e77fe517e74000a6f28  /app/maps/flow.json.gz' | sha256sum -c - && echo '28363ad1f70be20597547ef7c5159bb489f8b880be65a4a90066b765b84aae2b  /app/maps/fields.json.gz' | sha256sum -c - && echo 'f57c85894a47dac97762308b2be807bbf312206db182d4c33c067c5934ff4514  /app/maps/impact.json.gz' | sha256sum -c - && echo '18208f81d237a9531db05a00e5fb5406f21092ef43d4c4c392f786671c53c056  /app/maps/isolated.json.gz' | sha256sum -c - && echo '4c3bd48e7d1b430935bf36368db9e8bfec13736c9430e38045f8fd069199fd33  /app/maps/readers.json.gz' | sha256sum -c - && python -m compileall -q --invalidation-mode checked-hash /app/umbermill
RUN python /recipe/prepare_maps.py /app/maps /app/umbermill \
    && python /recipe/prepare_docs.py /app/base /app/umbermill \
    && python /recipe/prepare_index.py /app/maps/impact.json.gz /app/umbermill \
    && python /recipe/prepare_code.py /app/maps/impact.json.gz /app/umbermill /app/base
COPY tools/verify_saved.py tools/rule_witness.py tools/rule_graph.py tools/prepare_rules.py /app/rule-worker/
COPY maps/rule-inputs.tar.gz /recipe/rule-inputs.tar.gz
RUN echo 'bb02136afe3b0a3bb00cf76d0e09fb4d16dedbcbde70554a59c4a4b77619cf92  /recipe/rule-inputs.tar.gz' | sha256sum -c - \
    && python -m compileall -fq --invalidation-mode checked-hash /app/base/sqlglot /app/base/tests \
    && PYTHONHASHSEED=0 python /app/rule-worker/prepare_rules.py
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 IMPACT_GRANULARITY=function
RUN python -c "import subprocess; f=open('/app/maps/pristine-collection.json','wb'); subprocess.run(['python','-I','-B','/app/umbermill/import_snapshot_worker.py','--prepare','/app/base'],cwd='/app/base',stdout=f,user=65534,group=65534,check=True,timeout=65)"
COPY tools/prepare_executor.py /recipe/
RUN if ! python /recipe/prepare_executor.py; then rm -f /app/maps/executor-fixtures.sqlite; echo "Executor cache unavailable. Full native execution enabled."; fi
COPY tools/prepare_steps.py /recipe/
RUN timeout 180 python /recipe/prepare_steps.py \
    || (rm -f /app/maps/executor-steps.sqlite && echo "Using native executor steps")
COPY tools/collect_verification_units.py /app/tools/
RUN python -B /app/tools/collect_verification_units.py /app/base /app/maps --discover 12 --wall-seconds 38 --per-item-seconds 8 \
    || echo "Verification unit observations unavailable; complete methods remain eligible."
COPY tools/prepare_namespace.py /recipe/
RUN python /recipe/prepare_namespace.py /app/base /app/umbermill /app/maps \
    || rm -f /app/maps/namespace-inventory.json
USER 65534:65534
ENTRYPOINT ["python", "-m", "umbermill"]
