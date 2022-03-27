FROM eidos-service.di.unito.it/eidos-base-pytorch:1.10.0

COPY . /src
RUN chmod 775 /src
RUN chown -R :1337 /src

RUN chmod 775 /src/exp_results
RUN chown -R :1337 /src/exp_results

WORKDIR /src

ENTRYPOINT ["python3"]