#!/bin/sh

docker build  -t eidos-service.di.unito.it/barbano/bias-contrastive-learning:latest .
docker push eidos-service.di.unito.it/barbano/bias-contrastive-learning:latest
