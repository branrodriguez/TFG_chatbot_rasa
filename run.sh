#!/bin/bash
sudo service docker start  * Starting Docker: docker
ls -l
cd /home/ec2-user/rasa/git/TFG_chatbot_rasa
docker-compose up -d