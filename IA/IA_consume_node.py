import os
import settings
import json
import logging

from IA.utils import get_with_retry

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)
HERE = os.path.dirname(os.path.abspath(__file__))


def consume_node(guid: str, directory: str, bearer_token: str):
    path = os.path.join(HERE, directory, guid)
    if not os.path.exists(path):
        os.mkdir(path)

    path = os.path.join(path, 'node')

    try:
        os.mkdir(path)
    except FileExistsError:
        pass

    if bearer_token:
        auth_header = {'Authorization': f'Bearer {bearer_token}'}
    else:
        auth_header = {}

    url = f'{settings.OSF_API_URL}{settings.OSF_GUIDS_URL}{guid}'
    response = get_with_retry(url, retry_on=(429, ), headers=auth_header)
    json_file = os.path.join(path, f'{guid}.json')
    json_data = response.json()['data']
    with open(json_file, 'w') as json_write:
        json.dump(json_data, json_write)

    print("Node consumption complete!")