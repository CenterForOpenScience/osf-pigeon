import os
import json
import internetarchive
import pytest
from osf_pigeon import settings

import tempfile
from osf_pigeon.pigeon import (
    stream_files_to_dir,
    dump_json_to_dir,
    get_metadata_for_ia_item,
    get_additional_contributor_info,
    sync_metadata,
    upload,
    write_datacite_metadata,
)
from aioresponses import aioresponses

HERE = os.path.dirname(os.path.abspath(__file__))


def test_urllib3_compatibility():
    # internetarchive==1.9.9's get_session calls
    # Retry.DEFAULT_METHOD_WHITELIST, which is removed in urllib3 2.0.
    # requirements.txt now pins urllib3<2.
    # This test ensures that the pinned version of urllib3 is compatible with internetarchive==1.9.9.
    session = internetarchive.get_session(
        config={
            "s3": {"access": settings.IA_ACCESS_KEY, "secret": settings.IA_SECRET_KEY},
        },
    )
    assert session is not None


@pytest.mark.asyncio
class TestStreamFilesToDir:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def zip_name(self):
        return "archived_files.zip"

    @pytest.fixture
    def zip_data(self):
        return b"Brian Dawkins on game day"

    async def test_stream_files_to_dir(self, guid, zip_name, zip_data):
        with tempfile.TemporaryDirectory() as temp_dir:
            with aioresponses() as m:
                m.get(
                    f"{settings.OSF_FILES_URL}v1/resources/{guid}/providers/osfstorage/?zip=",
                    body=zip_data,
                )
                await stream_files_to_dir(
                    f"{settings.OSF_FILES_URL}v1/resources/{guid}/providers/osfstorage/?zip=",
                    temp_dir,
                    zip_name,
                )

            assert len(os.listdir(temp_dir)) == 1
            assert os.listdir(temp_dir)[0] == zip_name
            assert open(os.path.join(temp_dir, zip_name), "rb").read() == zip_data


@pytest.mark.asyncio
class TestDumpJSONFilesToDir:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def file_name(self):
        return "info.json"

    @pytest.fixture
    def json_data(self):
        with open(
            os.path.join(HERE, "fixtures/metadata-resp-with-embeds.json"), "rb"
        ) as fp:
            return fp.read()

    async def test_dump_json_to_dir(self, guid, json_data, file_name):
        with aioresponses() as m:
            m.get(f"{settings.OSF_API_URL}v2/guids/{guid}", body=json_data)
            with tempfile.TemporaryDirectory() as temp_dir:
                await dump_json_to_dir(
                    f"{settings.OSF_API_URL}v2/guids/{guid}", temp_dir, file_name
                )
                assert len(os.listdir(temp_dir)) == 1
                assert os.listdir(temp_dir)[0] == file_name
                info = json.loads(open(os.path.join(temp_dir, file_name)).read())
                assert info == json.loads(json_data)


@pytest.mark.asyncio
class TestDumpJSONFilesToDirMultipage:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def file_name(self):
        return "wikis.json"

    @pytest.fixture
    def page1(self):
        with open(
            os.path.join(HERE, "fixtures/wiki-metadata-response-page-1.json"), "r"
        ) as fp:
            return fp.read()

    @pytest.fixture
    def page2(self):
        with open(
            os.path.join(HERE, "fixtures/wiki-metadata-response-page-2.json"), "r"
        ) as fp:
            return fp.read()

    @pytest.fixture
    def expected_json(self, page1, page2):
        page1 = json.loads(page1)
        page2 = json.loads(page2)

        return page1["data"] + page2["data"]

    async def test_stream_files_to_dir(
        self, guid, page1, page2, file_name, expected_json
    ):
        with aioresponses() as m:
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/{guid}/wikis/",
                body=page1,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/{guid}/wikis/?page=2",
                body=page2,
            )
            with tempfile.TemporaryDirectory() as temp_dir:
                await dump_json_to_dir(
                    f"{settings.OSF_API_URL}v2/registrations/{guid}/wikis/",
                    temp_dir,
                    file_name,
                )
                assert len(os.listdir(temp_dir)) == 1
                assert os.listdir(temp_dir)[0] == file_name
                info = json.loads(open(os.path.join(temp_dir, file_name)).read())
                assert len(info) == 11
                assert info == expected_json


@pytest.mark.asyncio
class TestContributors:
    @pytest.fixture
    def guid(self):
        return "ft3ae"

    @pytest.fixture
    def file_name(self):
        return "contributors.json"

    @pytest.fixture
    def contributors_file(self):
        with open(os.path.join(HERE, "fixtures/ft3ae-contributors.json"), "r") as fp:
            return fp.read()

    @pytest.fixture
    def institutions_file(self):
        with open(os.path.join(HERE, "fixtures/ft3ae-institutions.json"), "r") as fp:
            return fp.read()

    async def test_stream_files_to_dir_with_contribs(
        self, guid, contributors_file, institutions_file, file_name
    ):
        with aioresponses() as m:

            m.get(
                f"{settings.OSF_API_URL}v2/registrations/{guid}/contributors/",
                body=contributors_file,
            )
            m.get(
                "http://localhost:8000/v2/users/s3rbx/institutions/",
                body=institutions_file,
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                await dump_json_to_dir(
                    f"{settings.OSF_API_URL}v2/registrations/{guid}/contributors/",
                    temp_dir,
                    file_name,
                    parse_json=get_additional_contributor_info,
                )
                assert len(os.listdir(temp_dir)) == 1
                assert os.listdir(temp_dir)[0] == file_name
                info = json.loads(open(os.path.join(temp_dir, file_name)).read())[
                    "data"
                ]
                assert len(info) == 1
                assert (
                    info[0]["embeds"]["users"]["data"]["attributes"]["social"]["orcid"]
                    == "0000-0001-4934-3444"
                )
                assert info[0]["affiliated_institutions"] == ["Center For Open Science"]


@pytest.mark.asyncio
class TestDatacite:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def metadata(self):
        with open(
            os.path.join(HERE, "fixtures/metadata-resp-with-embeds.json"), "rb"
        ) as fp:
            return json.loads(fp.read())

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    async def test_get_datacite_metadata(self, guid, mock_datacite, temp_dir, metadata):
        with aioresponses() as m:
            m.get(
                "https://mds.test.datacite.org/metadata/10.70102/osf.io/guid0",
                status=200,
                body=b"pretend this is XML.",
            )
            xml = await write_datacite_metadata(guid, temp_dir, metadata)
            assert xml == "pretend this is XML."


@pytest.mark.asyncio
class TestMetadata:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def metadata(self):
        with open(
            os.path.join(HERE, "fixtures/metadata-resp-with-embeds.json"), "rb"
        ) as fp:
            return json.loads(fp.read())

    @pytest.fixture
    def registration_children_sparse(self):
        with open(
            os.path.join(HERE, "fixtures/sparse-registration-children.json"), "rb"
        ) as fp:
            return fp.read()

    @pytest.fixture
    def test_node_json(self, temp_dir):
        os.mkdir(os.path.join(temp_dir, "data"))
        with open(
            os.path.join(HERE, "fixtures/metadata-resp-with-embeds.json"), "rb"
        ) as json_fp:
            with open(os.path.join(temp_dir, "data", "test.json"), "wb") as fp:
                fp.write(json_fp.read())
        yield

    @pytest.fixture
    def institutions_json(self):
        with open(os.path.join(HERE, "fixtures/institutions.json"), "rb") as fp:
            return fp.read()

    @pytest.fixture
    def subjects_json(self):
        with open(os.path.join(HERE, "fixtures/subjects.json"), "rb") as fp:
            return fp.read()

    @pytest.fixture
    def biblio_contribs(self):
        with open(os.path.join(HERE, "fixtures/biblio-contribs.json"), "rb") as fp:
            return fp.read()

    async def test_format_metadata_for_ia_item(
        self,
        metadata,
        registration_children_sparse,
        institutions_json,
        biblio_contribs,
        subjects_json,
    ):
        with aioresponses() as m:
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/pkdm6/children/"
                f"?fields%5Bregistrations%5D=id",
                body=registration_children_sparse,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/contributors/"
                f"?filter%5Bbibliographic%5D=true",
                body=biblio_contribs,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/institutions/",
                body=institutions_json,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/subjects/",
                body=subjects_json,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/children/",
                body=registration_children_sparse,
            )
            metadata = await get_metadata_for_ia_item(metadata)
            assert metadata == {
                "title": "Test Component",
                "description": "Test Description",
                "date": "2017-12-20",
                "publisher": "Center for Open Science",
                "osf_category": "",
                "license": "https://creativecommons.org/publicdomain/zero/1.0/legalcode",
                "osf_tags": [],
                "creator": ["John Tordoff"],
                "osf_subjects": ["Life Sciences"],
                "article_doi": "",
                "osf_registration_doi": "10.70102/osf.io/guid0",
                "children": [
                    f"https://archive.org/details/osf-registrations-hu68d-{settings.ID_VERSION}",
                    f"https://archive.org/details/osf-registrations-puxmb-{settings.ID_VERSION}",
                ],
                "osf_registry": "OSF Registries",
                "osf_registration_schema": "Open-Ended Registration",
                "source": "http://localhost:5000/8gqkv/",
                "osf_project": "http://localhost:8000/v2/nodes/g752b/",
                "affiliated_institutions": ["The Center For Open Science [Stage]"],
                "parent": f"https://archive.org/details/osf-registrations-dgkjr-"
                f"{settings.ID_VERSION}",
            }

    def test_modify_metadata_only(self, mock_ia_client, guid):
        metadata = {
            "title": "Test Component",
            "description": "Test Description",
            "date": "2017-12-20",
        }
        sync_metadata(guid, metadata)
        mock_ia_client.session.get_item.assert_called_with(
            f"osf-registrations-guid0-{settings.ID_VERSION}"
        )
        mock_ia_client.item.modify_metadata.assert_called_with(metadata)

    def test_modify_metadata_not_public(self, mock_ia_client, guid):
        metadata = {
            "title": "Test Component",
            "description": "Test Description",
            "date": "2017-12-20",
            "withdrawal_justification": "We're talkin' about practice!",
        }
        sync_metadata(guid, metadata)
        mock_ia_client.session.get_item.assert_called_with(
            f"osf-registrations-guid0-{settings.ID_VERSION}"
        )

        metadata["noindex"] = True
        metadata[
            "description"
        ] = "Note this registration has been withdrawn: \nTest Description"

        mock_ia_client.item.modify_metadata.assert_any_call({"noindex": True})
        mock_ia_client.item.modify_metadata.assert_any_call(metadata)


@pytest.mark.asyncio
class TestUpload:
    @pytest.fixture
    def guid(self):
        return "guid0"

    @pytest.fixture
    def metadata(self):
        with open(
            os.path.join(HERE, "fixtures/metadata-resp-with-embeds.json"), "rb"
        ) as fp:
            return json.loads(fp.read())

    @pytest.fixture
    def temp_dir(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            yield temp_dir

    @pytest.fixture
    def registration_children_sparse(self):
        with open(
            os.path.join(HERE, "fixtures/sparse-registration-children.json"), "rb"
        ) as fp:
            return fp.read()

    @pytest.fixture
    def institutions_json(self):
        with open(os.path.join(HERE, "fixtures/institutions.json"), "rb") as fp:
            return fp.read()

    @pytest.fixture
    def biblio_contribs(self):
        with open(os.path.join(HERE, "fixtures/biblio-contribs.json"), "rb") as fp:
            return fp.read()

    @pytest.fixture
    def subjects_json(self):
        with open(os.path.join(HERE, "fixtures/subjects.json"), "rb") as fp:
            return fp.read()

    async def test_upload(
        self,
        mock_ia_client,
        guid,
        registration_children_sparse,
        biblio_contribs,
        metadata,
        institutions_json,
        subjects_json,
        temp_dir,
    ):
        with aioresponses() as m:
            m.add(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/children/",
                body=registration_children_sparse,
            )
            m.add(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/contributors/"
                f"?filter%5Bbibliographic%5D=true",
                body=biblio_contribs,
            )
            m.add(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/institutions/",
                body=institutions_json,
            )
            m.add(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/subjects/",
                body=subjects_json,
            )
            await upload(
                guid,
                temp_dir,
                metadata,
            )

            mock_ia_client.session.get_item.assert_called_with("guid0")
            mock_ia_client.item.upload.assert_called_with(
                f"{temp_dir}/bag.zip",
                metadata={
                    "collection": f"osf-registration-providers-osf-{settings.ID_VERSION}",
                    "publisher": "Center for Open Science",
                    "osf_registration_doi": "10.70102/osf.io/guid0",
                    "title": "Test Component",
                    "description": "Test Description",
                    "osf_category": "",
                    "osf_tags": [],
                    "date": "2017-12-20",
                    "article_doi": "",
                    "osf_registry": "OSF Registries",
                    "osf_registration_schema": "Open-Ended Registration",
                    "source": "http://localhost:5000/8gqkv/",
                    "osf_project": "http://localhost:8000/v2/nodes/g752b/",
                    "creator": ["John Tordoff"],
                    "affiliated_institutions": ["The Center For Open Science [Stage]"],
                    "osf_subjects": ["Life Sciences"],
                    "children": [
                        f"https://archive.org/details/"
                        f"osf-registrations-hu68d-{settings.ID_VERSION}",
                        f"https://archive.org/details/"
                        f"osf-registrations-puxmb-{settings.ID_VERSION}",
                    ],
                    "parent": f"https://archive.org/details/"
                    f"osf-registrations-dgkjr-{settings.ID_VERSION}",
                    "license": "https://creativecommons.org/publicdomain/zero/1.0/legalcode",
                },
                secret_key=settings.IA_SECRET_KEY,
                access_key=settings.IA_ACCESS_KEY,
            )

    async def test_upload_with_different_provider(
        self,
        mock_ia_client,
        guid,
        registration_children_sparse,
        biblio_contribs,
        metadata,
        temp_dir,
        institutions_json,
        subjects_json,
    ):
        """
        Different providers should get uploaded to different collections
        """
        metadata["data"]["embeds"]["provider"]["data"]["id"] = "burds"
        with aioresponses() as m:
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/children/",
                body=registration_children_sparse,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/contributors/"
                f"?filter%5Bbibliographic%5D=true",
                body=biblio_contribs,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/institutions/",
                body=institutions_json,
            )
            m.get(
                f"{settings.OSF_API_URL}v2/registrations/8gqkv/subjects/",
                body=subjects_json,
            )
            await upload(
                guid,
                temp_dir,
                metadata,
            )
            mock_ia_client.session.get_item.assert_called_with("guid0")
            mock_ia_client.item.upload.assert_called_with(
                f"{temp_dir}/bag.zip",
                metadata={
                    "collection": f"osf-registration-providers-burds-{settings.ID_VERSION}",
                    "publisher": "Center for Open Science",
                    "osf_registration_doi": "10.70102/osf.io/guid0",
                    "title": "Test Component",
                    "description": "Test Description",
                    "osf_category": "",
                    "osf_tags": [],
                    "date": "2017-12-20",
                    "article_doi": "",
                    "osf_registry": "OSF Registries",
                    "osf_registration_schema": "Open-Ended Registration",
                    "source": "http://localhost:5000/8gqkv/",
                    "osf_project": "http://localhost:8000/v2/nodes/g752b/",
                    "creator": ["John Tordoff"],
                    "affiliated_institutions": ["The Center For Open Science [Stage]"],
                    "osf_subjects": ["Life Sciences"],
                    "children": [
                        f"https://archive.org/details/"
                        f"osf-registrations-hu68d-{settings.ID_VERSION}",
                        f"https://archive.org/details/"
                        f"osf-registrations-puxmb-{settings.ID_VERSION}",
                    ],
                    "parent": f"https://archive.org/details/"
                    f"osf-registrations-dgkjr-{settings.ID_VERSION}",
                    "license": "https://creativecommons.org/publicdomain/zero/1.0/legalcode",
                },
                secret_key=settings.IA_SECRET_KEY,
                access_key=settings.IA_ACCESS_KEY,
            )
