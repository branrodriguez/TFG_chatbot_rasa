cd app/
# Start rasa server with nlu model
rasa run --enable-api --cors "*" --debug && rasa run actions