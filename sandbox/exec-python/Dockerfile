# Tier B: the code-execution sandbox.
#
# Model-authored Python runs here and nowhere else. It is a separate image from
# the persona runtime on purpose: this tier holds no credentials, has no network
# at all, and never shares a process with the code that does.

FROM python:3.13-slim-bookworm AS runtime

# Preinstalled so a snippet never needs to fetch anything -- which is just as
# well, since this tier has no route out.
RUN pip install --no-cache-dir \
      "pandas==2.3.3" \
      "numpy==2.3.4" \
      "matplotlib==3.10.7" \
 && rm -rf /root/.cache

RUN groupadd --gid 1001 exec && useradd --uid 1001 --gid 1001 --create-home exec

COPY runner.py /opt/runner.py
COPY server.py /opt/server.py
RUN chmod 0444 /opt/runner.py /opt/server.py

USER 1001
WORKDIR /work
EXPOSE 8080

# Accepts a job, runs it, returns the result. The only way into this tier --
# it has no egress at all.
CMD ["python", "/opt/server.py"]
