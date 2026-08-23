# Static corpus service for search_corpus.
#
# Deliberately offline: a demo that reaches the live internet is not
# reproducible, and would need sandbox egress to arbitrary hosts.
FROM python:3.13-slim AS runtime
RUN groupadd --gid 1001 corpus && useradd --uid 1001 --gid 1001 --create-home corpus
WORKDIR /srv
COPY server.py corpus.json ./
RUN chmod 0444 server.py corpus.json
USER 1001
EXPOSE 8080
CMD ["python", "server.py"]
