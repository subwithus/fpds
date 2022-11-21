"""
Base classes for FPDS XML elements

author: derek663@gmail.com
last_updated: 11/20/2022
"""

import re
import xml
from itertools import chain
from typing import Dict, List, NoReturn, Union
from xml.etree import ElementTree
from xml.etree.ElementTree import Element

import requests
from tqdm import tqdm

from fpds.config import FPDS_FIELDS_CONFIG as FIELDS
from fpds.utilities import filter_config_dict, raw_literal_regex_match

# types
TREE = xml.etree.ElementTree.Element

NAMESPACE_REGEX = r"\{(.*)\}"
WHITESPACE_REGEX = r"\n\s+"
LAST_PAGE_REGEX = r"start=(.*?)$"

class fpdsMixin:
    @property
    def url_base(self) -> str:
        return "https://www.fpds.gov/ezsearch/FEEDS/ATOM?FEEDNAME=PUBLIC"

    @staticmethod
    def convert_to_lxml_tree(content):
        """Returns lxml tree element from a bytes response
        """
        tree = ElementTree.fromstring(content)
        return tree

class _ElementAttributes(Element):
    """
    Utility class that helps parse out extra features of XML tags generated
    by `xml.etree.ElementTree.Element`. This class should ideally not be
    instantiated by users.

    Parameters
    ----------
    element: xml.etree.ElementTree.Element
        An XML element
    namespace_dict: Dict[str, str]
        A namespace dictionary that allows module to parse FPDS elements
    """
    def __init__(
        self,
        element: Element,
        namespace_dict: Dict[str, str]
    ) -> "_ElementAttributes":
        self.element = element
        self.namespace_dict = namespace_dict

    @property
    def clean_tag(self) -> str:
        """Tag name without the namespace. A tag like the following:
        `ns1:productOrServiceInformation` would simply return
        `productOrServiceInformation`
        """
        namespaces = "|".join(self.namespace_dict.values())
        # yeah, f-strings don't do well with backslashes
        PATTERN = "\{(" + namespaces + ")\}"
        clean_tag = re.sub(PATTERN, "", self.element.tag)
        return clean_tag

    def _generate_nested_attribute_dict(self) -> Dict[str, str]:
        """Returns all attributes of an Element

        Example
        -------
        <ns1:contractActionType description="BPA" part8OrPart13="PART8">E</ns1:contractActionType>

        The value of the `contractActionType` is "E". To help decipher
        this data, this class will parse out all attributes of the tag. This
        method will generate a nested key name structure to indicate what tag
        each attribute belongs to. In this example, the tag `contractActionType`
        has two attributes: `description` and `part8OrPart13`. This method will
        represent this tag the following way:

            {
                "contractActionType": "E",
                "contractActionType__description": "BPA"
                "contractActionType__part8OrPart13": "PART8"
            }
        """
        attributes = self.element.attrib
        _attributes_copy = attributes.copy()

        tag = self.clean_tag
        for key in attributes:
            nested_key = f"{tag}__{key}"
            _attributes_copy[nested_key] = attributes[key]
            del _attributes_copy[key]
        _attributes_copy[f"{tag}"] = self.element.text
        return _attributes_copy


class fpdsRequest(fpdsMixin):
    """Makes a GET request to the FPDS ATOM feed. Takes an unlimited number of
    arguments. All query parameters should be submitted as strings. During
    class instantiation, this class will validate argument names and values and
    raise a `ValueError` if any error exists.

    Example:
        request = fpdsRequest(
            LAST_MOD_DATE="[2022/01/01, 2022/05/01]",
            AGENCY_CODE="7504"
        )

    Parameters
    ----------
    cli_run: bool
        Flag indicating if this class is being isntantiated by a CLI run
        Defaults to `False`
    """
    def __init__(self, cli_run: bool = False,  **kwargs):
        self.cli_run = cli_run
        if kwargs:
            self.kwargs = kwargs
        else:
            raise ValueError("You must provide at least one keyword parameter")

        # do not run class validations since CLI command has its own
        if not self.cli_run:
            self.valid_fields = [field.get("name") for field in FIELDS]
            for kwarg, value in self.kwargs.items():
                if kwarg not in self.valid_fields:
                    raise ValueError(f"`{kwarg}` is not a valid FPDS parameter")
                else:
                    kwarg_dict = filter_config_dict(FIELDS, "name", kwarg)
                    kwarg_regex = kwarg_dict.get("regex")
                    match = raw_literal_regex_match(kwarg_regex, value)
                    if not match:
                        raise ValueError(
                            f"`{value}` does not match regex: {kwarg_regex}"
                        )
                    if kwarg_dict.get("quotes"):
                        self.kwargs[kwarg] = f'"{value}"'

    def __str__(self):
        kwargs_str = " ".join(
            [f"{key}={value}" for key, value in self.kwargs.items()]
        )
        return f"<fpdsRequest {kwargs_str}>"

    def __call__(self):
        records = self.parse_content()
        return records

    @property
    def search_params(self):
        """Search parameters inputted by user"""
        _params = [f"{key}:{value}" for key, value in self.kwargs.items()]
        return " ".join(_params)

    def send_request(self, url: str = None) -> NoReturn:
        """Sends request to FPDS Atom feed

        Parameters
        ----------
        url: str, optional
            A URL to send a GET request to. If not provided, this method
            will default to using `url_base`
        """
        response = requests.get(
            url=self.url_base if not url else url,
            params={"q": self.search_params}
        )
        response.raise_for_status()
        content_tree = self.convert_to_lxml_tree(response.content)

        if "content" not in self.__dict__.keys():
            self.content = [content_tree]
        else:
            self.content.append(content_tree)

    def create_content_iterable(self) -> NoReturn:
        """Paginates through response and creates an iterable of XML trees.
        This method will not have a return but rather, will set the `content`
        attribute to an iterable of XML ElementTree's
        """
        self.send_request()
        params = self.search_params
        tree = fpdsXML(self.content[0])

        links = tree.pagination_links(params=params)
        links.pop(0) # the first link is the first page so we drop it
        for link in links:
            self.send_request(link)

    def parse_content(self) -> List[Dict[str, Union[str, int, float]]]:
        """Parses a content iterable and generates a list of records
        """
        self.create_content_iterable()

        records = []
        for tree in tqdm(self.content):
            xml = fpdsXML(tree)
            records.append(xml.get_entry_data())
        return list(chain.from_iterable(records))


# TODO: have class inherit from Logger()
class fpdsXML(fpdsMixin):
    """Parses FPDS request content received as bytes or `xml.etree.ElementTree`.

    Parameters
    ----------
    content
    """
    def __init__(self, content: Union[bytes, TREE]) -> "fpdsXML":
        self.content = content
        if isinstance(self.content, bytes):
            self.tree = self.convert_to_lxml_tree()
        if isinstance(self.content, TREE):
            self.tree = content
        if not isinstance(self.content, (bytes, TREE)):
            raise TypeError(
                "You must provide bytes content or an instance of"
                "`xml.etree.ElementTree.Element`"
            )

    def parse_items(self, element: Element):
        """Returns iteration of `Element` as a generator
        """
        yield from element.iter()

    def convert_to_lxml_tree(self) -> TREE:
        """Returns lxml tree element from a bytes response
        """
        tree = ElementTree.fromstring(self.content)
        return tree

    @staticmethod
    def _get_full_namespace(element: Element) -> str:
        """For some odd reason, the lxml API doesn't have a method to provide
        namespaces natively unless an XML file is saved locally. To avoid this,
        we just do some regex work

        Parameters
        ----------
        element: Element
            An lxml Element type
        """
        namespace = re.match(NAMESPACE_REGEX, element.tag)
        return namespace.group(1) if namespace else ''

    @property
    def response_size(self) -> int:
        """Max number of records in a single response
        """
        return 10

    @property
    def namespace_dict(self) -> Dict[str, str]:
        """The better way of parsing tree elements with namespaces, per the docs.
        Note that `namespaces` is a list, which retains parsing order of the
        tree, which will be important in identifying Atom entries in `fpds`

        https://docs.python.org/3/library/xml.etree.elementtree.html#parsing-xml-with-namespaces
        """
        namespaces = list()
        for element in self.parse_items(self.tree):
            _namespace = self._get_full_namespace(element)
            if _namespace not in namespaces:
                namespaces.append(_namespace)

        namespace_dict = {f'ns{idx}': ns for idx, ns in enumerate(namespaces)}
        return namespace_dict

    @property
    def total_record_count(self) -> int:
        """Total number of records for response
        """
        links = self.tree.findall('.//ns0:link', self.namespace_dict)
        last_link = [link for link in links if link.get("rel") == "last"]
        # index 0 should work since only one link should match the filter cond.
        record_count = re.search(LAST_PAGE_REGEX, last_link[0].attrib["href"])
        return int(record_count.group(1))

    def pagination_links(self, params: str) -> List[str]:
        """FPDS contains an XML tag that provides the last link of the response.
        Within that link is the total number of records contained within the
        response. This method uses that value to build the pagination links
        """
        resp_size = self.response_size
        page_range = list(
            range(0, self.total_record_count + resp_size, resp_size)
        )
        page_links = []
        for num in page_range:
            link = f"{self.url_base}&q={params}&start={num}"
            page_links.append(link)
        return page_links

    def get_atom_feed_entries(self) -> List[TREE]:
        """Returns tree entries that contain FPDS record data
        """
        data_entries = self.tree.findall(
            './/ns0:entry',
            self.namespace_dict
        )
        return data_entries

    def get_entry_data(self):
        entries = self.get_atom_feed_entries()

        parsed_records = []
        for entry in entries:
            entry_tags = dict()
            tags = self.parse_items(entry)
            for tag in tags:
                elem = _ElementAttributes(tag, self.namespace_dict)
                entry_tags.update(elem._generate_nested_attribute_dict())
            parsed_records.append(entry_tags)

        return parsed_records
