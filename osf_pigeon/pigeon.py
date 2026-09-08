import os
import re
import math
import json
import tempfile
import zipfile
import bagit
import asyncio
from datetime import datetime
from asyncio import events
from ratelimit import sleep_and_retry
from ratelimit.exception import RateLimitException
from aiohttp import ClientSession, ClientTimeout, http_exceptions

import internetarchive
from datacite import DataCiteMDSClient
from datacite.errors import DataCiteNotFoundError

from osf_pigeon import settings


async def stream_files_to_dir(from_url, to_dir, name):
    async with ClientSession(timeout=ClientTimeout(total=settings.FILES_TIMEOUT)) as session:
        async with session.get(from_url) as resp:
            with open(os.path.join(to_dir, name), "wb") as fp:
                async for chunk in resp.content.iter_any():
                    fp.write(chunk)


async def dump_json_to_dir(from_url, to_dir, name, parse_json=None):
    pages = await get_paginated_data(from_url, parse_json)
    with open(os.path.join(to_dir, name), "w") as fp:
        json.dump(pages, fp)

    return pages


def create_zip(temp_dir):
    with zipfile.ZipFile(os.path.join(temp_dir, "bag.zip"), "w") as fp:
        for root, dirs, files in os.walk(os.path.join(temp_dir, "bag")):
            for file in files:
                file_path = os.path.join(root, file)
                file_name = re.sub(f"^{temp_dir}", "", file_path)
                fp.write(file_path, arcname=file_name)


async def get_relationship_attribute(key, url, func):
    data = await get_paginated_data(url)
    if "data" in data:
        return {key: list(map(func, data["data"]))}
    return {key: list(map(func, data))}


def get_contributor_info(contrib):
    errors = contrib["embeds"]["users"].get('errors')
    if errors and errors[0]['detail'] == 'The requested user is no longer available.':
        return errors[0]['meta']['full_name']
    else:
        return contrib["embeds"]["users"]["data"]["attributes"]["full_name"]


async def get_metadata_for_ia_item(json_metadata):
    """
    This is meant to take the response JSON metadata and format it for IA buckets, this is not
    used to generate JSON to be uploaded as raw data into the buckets.
    :param json_metadata: metadata from OSF registration view contains attributes and relationship
    urls.

    Note: Internet Archive advises that all metadata that points to internal OSF features should
    have a specific `osf_` prefix. Example: `registry` should be `osf_registry`, however metadata
    such as affiliated_institutions is self-explanatory and doesn't need a prefix.

    :return: ia_metadata the metadata for an IA bucket. Should include the following if they are
     not null:
        - publisher
        - title
        - description
        - date
        - osf_category
        - osf_subjects
        - osf_tags
        - osf_registration_doi
        - osf_registry
        - osf_registration_schema
        - creator (biblographic contributors, IA recommended this keyword)
        - article_doi
        - parent
        - children
        - source
        - affiliated_institutions
        - license
    """
    relationship_data = [
        get_relationship_attribute(
            "creator",
            f'{settings.OSF_API_URL}v2/registrations/{json_metadata["data"]["id"]}/contributors/'
            f"?filter[bibliographic]=true",
            get_contributor_info,
        ),
        get_relationship_attribute(
            "affiliated_institutions",
            f'{settings.OSF_API_URL}v2/registrations/{json_metadata["data"]["id"]}/institutions/',
            lambda institution: institution["attributes"]["name"],
        ),
        get_relationship_attribute(
            "osf_subjects",
            f'{settings.OSF_API_URL}v2/registrations/{json_metadata["data"]["id"]}/subjects/',
            lambda subject: subject["attributes"]["text"],
        ),
        get_relationship_attribute(
            "children",
            f'{settings.OSF_API_URL}v2/registrations/{json_metadata["data"]["id"]}/children/',
            lambda child: f"https://archive.org/details/"
            f'{settings.REG_ID_TEMPLATE.format(guid=child["id"])}',
        ),
    ]

    relationship_data = {
        k: v
        for pair in await asyncio.gather(*relationship_data)
        for k, v in pair.items()
    }  # merge all the pairs

    parent = json_metadata["data"]["relationships"]["parent"]["data"]
    if parent:
        relationship_data[
            "parent"
        ] = f"https://archive.org/details/{settings.REG_ID_TEMPLATE.format(guid=parent['id'])}"

    embeds = json_metadata["data"]["embeds"]

    if not embeds["license"].get(
        "errors"
    ):  # The reported error here is just a 404, so ignore if no license
        relationship_data["license"] = embeds["license"]["data"]["attributes"]["url"]

    doi = next(
        (
            identifier["attributes"]["value"]
            for identifier in embeds["identifiers"]["data"]
            if identifier["attributes"]["category"] == "doi"
        ),
        None,
    )
    attributes = json_metadata["data"]["attributes"]
    article_doi = json_metadata["data"]["attributes"]["article_doi"]
    ia_metadata = {
        "publisher": "Center for Open Science",
        "osf_registration_doi": doi,
        "title": attributes["title"],
        "description": attributes["description"],
        "osf_category": attributes["category"],
        "osf_tags": attributes["tags"],
        "date": str(
            datetime.strptime(
                attributes["date_created"], "%Y-%m-%dT%H:%M:%S.%fZ"
            ).date()
        ),
        "article_doi": f"urn:doi:{article_doi}" if article_doi else "",
        "osf_registry": embeds["provider"]["data"]["attributes"]["name"],
        "osf_registration_schema": embeds["registration_schema"]["data"]["attributes"][
            "name"
        ],
        "osf_project":
            json_metadata["data"]["relationships"]["registered_from"]["links"]["related"]["href"],
        "source": json_metadata["data"]["links"]["html"],
        **relationship_data,
    }
    return ia_metadata


async def write_datacite_metadata(guid, temp_dir, metadata):
    try:
        doi = next(
            (
                identifier["attributes"]["value"]
                for identifier in metadata["data"]["embeds"]["identifiers"]["data"]
                if identifier["attributes"]["category"] == "doi"
            )
        )
    except StopIteration:
        raise DataCiteNotFoundError(
            f"Datacite DOI not found for registration {guid} on OSF server."
        )
    client = DataCiteMDSClient(
        url=settings.DATACITE_URL,
        username=settings.DATACITE_USERNAME,
        password=settings.DATACITE_PASSWORD,
        prefix=settings.DATACITE_PREFIX,
    )
    try:
        xml_metadata = client.metadata_get(doi)
    except DataCiteNotFoundError:
        raise DataCiteNotFoundError(
            f"Datacite DOI {doi} not found for registration {guid} on Datacite server."
        )

    with open(os.path.join(temp_dir, "datacite.xml"), "w") as fp:
        fp.write(xml_metadata)

    return xml_metadata


@sleep_and_retry
async def get_with_retry(url, retry_on=(), sleep_period=None, headers=None):
    if not headers:
        headers = {}

    if settings.OSF_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {settings.OSF_BEARER_TOKEN}"

    async with ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status in retry_on:
                raise RateLimitException(
                    message="Too many requests, sleeping.",
                    period_remaining=sleep_period
                    or int(resp.headers.get("Retry-After") or 0),
                )  # This will be caught by @sleep_and_retry and retried
            resp.raise_for_status()
            return await resp.json()


async def get_pages(url, page, result=None, parse_json=None, semaphore=None):
    if result is None:
        result = {}
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}page={page}"
    data = {}
    if semaphore is None:
        data = await get_with_retry(url, retry_on=(429,))
    else:
        async with semaphore:
            data = await get_with_retry(url, retry_on=(429,))

    result[page] = data["data"]

    if parse_json:
        result[page] = parse_json(data)["data"]

    return result


async def get_additional_contributor_info(response):
    contributor_data_list = []
    for contributor in response["data"]:
        contributor_data = {}
        errors = contributor["embeds"]["users"].get('errors')
        if errors and errors[0]['detail'] == 'The requested user is no longer available.':
            contributor_data = errors[0]['meta']
        else:
            embed_data = contributor["embeds"]["users"]["data"]
            institution_url = embed_data["relationships"]["institutions"]["links"][
                "related"
            ]["href"]
            data = await get_with_retry(institution_url)
            institution_data = data["data"]
            institution_list = [
                institution["attributes"]["name"] for institution in institution_data
            ]
            contributor_data["affiliated_institutions"] = institution_list

        contributor.update(contributor_data)
        contributor_data_list.append(contributor)
    response["data"] = contributor_data_list
    return response


async def get_paginated_data(url, parse_json=None):
    data = await get_with_retry(url, retry_on=(429,))
    tasks = []
    is_paginated = data.get("links", {}).get("next")

    if parse_json:
        data = await parse_json(data)

    if is_paginated:
        paging_semaphore = asyncio.Semaphore(settings.PAGING_SEMAPHORE)
        result = {1: data["data"]}
        total = data["links"].get("meta", {}).get("total") or data["meta"].get("total")
        per_page = data["links"].get("meta", {}).get("per_page") or data["meta"].get(
            "per_page"
        )

        pages = math.ceil(int(total) / int(per_page))
        for i in range(1, pages):
            task = get_pages(url, i + 1, result=result, semaphore=paging_semaphore)
            tasks.append(task)

        await asyncio.gather(*tasks)
        pages_as_list = []
        # through the magic of async all our pages have loaded.
        for page in list(result.values()):
            pages_as_list += page
        return pages_as_list
    else:
        return data


def get_ia_item(guid):
    session = internetarchive.get_session(
        config={
            "s3": {"access": settings.IA_ACCESS_KEY, "secret": settings.IA_SECRET_KEY},
        },
    )
    return session.get_item(guid)


def sync_metadata(guid, metadata):
    """
    This is used to sync the metadata of archive.org items with OSF Registrations.
    synced is as follows:
        - title
        - description
        - date
        - category
        - subjects
        - tags
        - affiliated_institutions
        - license
        - article_doi

    `moderation_state` is an allowable key, but only to determine a withdrawal status of a
     registration.
    :param guid:
    :param metadata:
    :return:
    """

    if not metadata:
        raise http_exceptions.PayloadEncodingError(
            "Metadata Payload not included in request"
        )

    valid_updatable_metadata_keys = [
        "title",
        "description",
        "date",
        "modified",
        "osf_category",
        "osf_subjects",
        "osf_tags",
        "article_doi",
        "affiliated_institutions",
        "license",
        "withdrawal_justification",
    ]

    invalid_keys = set(metadata.keys()).difference(set(valid_updatable_metadata_keys))
    if invalid_keys:
        raise http_exceptions.PayloadEncodingError(
            f"Metadata payload contained invalid tag(s): `{', '.join(list(invalid_keys))}`"
            f" not included in valid keys: `{', '.join(valid_updatable_metadata_keys)}`.",
        )

    item_name = settings.REG_ID_TEMPLATE.format(guid=guid)
    ia_item = get_ia_item(item_name)
    if not metadata.get("withdrawal_justification"):  # withdrawn == not searchable
        ia_item.modify_metadata(metadata)
    else:
        description = ia_item.metadata.get("description")
        if description:
            metadata[
                "description"
            ] = f"Note this registration has been withdrawn: \n{description}"
        else:
            metadata["description"] = "This registration has been withdrawn"

        ia_item.modify_metadata(metadata)
        ia_item.modify_metadata({"noindex": True})

    return ia_item, list(metadata.keys())


async def upload(item_name, temp_dir, metadata):
    ia_item = get_ia_item(item_name)
    ia_metadata = await get_metadata_for_ia_item(metadata)
    provider_id = metadata["data"]["embeds"]["provider"]["data"]["id"]
    ia_item.upload(
        os.path.join(temp_dir, "bag.zip"),
        metadata={
            "collection": settings.PROVIDER_ID_TEMPLATE.format(provider_id=provider_id),
            **ia_metadata,
        },
        access_key=settings.IA_ACCESS_KEY,
        secret_key=settings.IA_SECRET_KEY,
    )
    return ia_item


async def get_registration_metadata(guid, temp_dir, filename):
    metadata = await get_paginated_data(
        f"{settings.OSF_API_URL}v2/registrations/{guid}/"
        f"?embed=parent"
        f"&embed=provider"
        f"&embed=identifiers"
        f"&embed=license"
        f"&embed=registration_schema"
        f"&related_counts=true"
        f"&version=2.20"
    )
    if metadata["data"]["attributes"]["withdrawn"]:
        raise PermissionError(f"Registration {guid} is withdrawn")

    with open(os.path.join(temp_dir, filename), "w") as fp:
        json.dump(metadata, fp)

    return metadata


async def archive(guid):
    with tempfile.TemporaryDirectory(
        dir=settings.PIGEON_TEMP_DIR, prefix=settings.REG_ID_TEMPLATE.format(guid=guid)
    ) as temp_dir:
        os.mkdir(os.path.join(temp_dir, "bag"))
        # await first to check if withdrawn
        metadata = await get_registration_metadata(
            guid,
            os.path.join(
                temp_dir,
                "bag"
            ),
            "registration.json"
        )
        schema_metadata = metadata["data"]["relationships"]["registration_schema"]
        tasks = [
            write_datacite_metadata(guid, temp_dir, metadata),
            dump_json_to_dir(
                from_url=f"{settings.OSF_API_URL}v2/registrations/{guid}/logs/"
                f"?page[size]=100",
                to_dir=os.path.join(temp_dir, "bag"),
                name="logs.json",
            ),
            dump_json_to_dir(
                from_url=f"{settings.OSF_API_URL}v2/registrations/{guid}/contributors/"
                f"?page[size]=100",
                to_dir=os.path.join(temp_dir, "bag"),
                name="contributors.json",
                parse_json=get_additional_contributor_info,
            ),
            dump_json_to_dir(
                from_url=f"{settings.OSF_API_URL}v2/registrations/{guid}/schema_responses/"
                f"?page[size]=100",
                to_dir=os.path.join(temp_dir, "bag"),
                name="schema_responses.json",
            ),
            dump_json_to_dir(
                from_url=schema_metadata["links"]["related"]["href"],
                to_dir=os.path.join(temp_dir, "bag"),
                name="registration_schema.json",
            ),
        ]
        # only download archived data if there are files
        file_count = metadata["data"]["relationships"]["files"]["links"]["related"][
            "meta"
        ]["count"]
        if file_count:
            tasks.append(
                stream_files_to_dir(
                    f"{settings.OSF_FILES_URL}v1/resources/{guid}/providers/osfstorage/?zip=",
                    os.path.join(temp_dir, "bag"),
                    "archived_files.zip",
                )
            )

        wiki_enabled = metadata["data"]["attributes"]["wiki_enabled"]
        if wiki_enabled:
            tasks.append(
                dump_json_to_dir(
                    from_url=f"{settings.OSF_API_URL}v2/registrations/{guid}/wikis/"
                    f"?page[size]=100",
                    to_dir=os.path.join(temp_dir, "bag"),
                    name="wikis.json",
                )
            )

        await asyncio.gather(*tasks)

        # bagit changes the cwd so set it here again in case it crashed before changing it back.
        os.chdir(temp_dir)
        bagit.make_bag(os.path.join(temp_dir, "bag"))
        bag = bagit.Bag(os.path.join(temp_dir, "bag"))
        assert bag.is_valid()

        create_zip(temp_dir)
        ia_item = await upload(
            settings.REG_ID_TEMPLATE.format(guid=guid), temp_dir, metadata
        )

        return ia_item, guid


def run(coroutine):
    loop = events.new_event_loop()
    try:
        events.set_event_loop(loop)
        return loop.run_until_complete(coroutine)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            events.set_event_loop(None)
            loop.close()
