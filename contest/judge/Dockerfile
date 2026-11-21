FROM python:3.9.11-slim-bullseye@sha256:10f6e16011b32cca4e2c7aef249340b401a24b956b89bed761dcd6c99f997a08
WORKDIR /opt/contest
COPY contest/engine/*.py ./engine/
COPY contest/judge/*.py ./judge/
USER 10001:10001
ENTRYPOINT ["python", "-B", "-u", "-m", "judge"]
