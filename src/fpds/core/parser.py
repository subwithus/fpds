"""
Base classes for FPDS XML elements.

author: derek663@gmail.com
last_updated: 01/15/2024
"""
import asyncio
import multiprocessing
from asyncio import Semaphore
from typing import List, Union
from xml.etree.ElementTree import ElementTree, fromstring

import aiohttp
import urllib3
from aiohttp import ClientSession
from urllib3 import request

from fpds.core import FPDS_ENTRY
from fpds.core.mixins import fpdsMixin
from fpds.core.xml import fpdsXML
from fpds.utilities import validate_kwarg


class fpdsRequest(fpdsMixin):
    """Makes a GET request to the FPDS ATOM feed. Takes an unlimited number of
    arguments. All query parameters should be submitted as strings. If new
    arguments are added to the feed, add the argument to the
    `fpds/core/constants.json` file. During class instantiation, this class
    will validate argument names and values and raise a `ValueError` if any
    error exists.

    Example:
        request = fpdsRequest(
            LAST_MOD_DATE="[2022/01/01, 2022/05/01]",
            AGENCY_CODE="7504"
        )

    Parameters
    ----------
    cli_run: `bool`
        Flag indicating if this class is being isntantiated by a CLI run.
        Defaults to `False`.

    Raises
    ------
    ValueError:
        Raised if no keyword argument(s) are provided, a keyword argument
        is not a valid FPDS parameter, or the value of a keyword argument
        does not match the expected regex.
    """

    def __init__(self, cli_run: bool = False, **kwargs):
        self.cli_run = cli_run
        self.content = []  # type: List[ElementTree]
        self.links = []  # type: List[str]
        if kwargs:
            self.kwargs = kwargs
        else:
            raise ValueError("You must provide at least one keyword parameter")

        # do not run class validations since CLI command has its own
        if not self.cli_run:
            for kwarg, value in self.kwargs.items():
                self.kwargs[kwarg] = validate_kwarg(kwarg=kwarg, string=value)

    def __str__(self) -> str:
        """String representation of `fpdsRequest`"""
        kwargs_str = " ".join([f"{key}={value}" for key, value in self.kwargs.items()])
        return f"<fpdsRequest {kwargs_str}>"

    def __url__(self) -> str:
        """Custom magic method for request URL"""
        return f"{self.url_base}&q={self.search_params}"

    @property
    def search_params(self) -> str:
        """Search parameters inputted by user"""
        _params = [f"{key}:{value}" for key, value in self.kwargs.items()]
        return " ".join(_params)

    @staticmethod
    def convert_to_lxml_tree(content: Union[str, bytes]) -> ElementTree:
        """Returns lxml tree element from a bytes response"""
        tree = ElementTree(fromstring(content))
        return tree

    def initial_request(self):
        """Send initial request to FPDS Atom feed and returns first page."""
        pool = urllib3.PoolManager()
        encoded_params = request.urlencode({"q": self.search_params})
        response = pool.request("GET", f"{self.url_base}&{encoded_params}")
        content_tree = self.convert_to_lxml_tree(response.data.decode("utf-8"))
        self.content.append(content_tree)

    async def convert(self, session: ClientSession, link: str):
        async with session.get(link) as response:
            content = await response.read()
            xml = fpdsXML(content=self.convert_to_lxml_tree(content))
            return xml

    async def fetch(self):
        self.create_request_links()

        semaphore = Semaphore(10)
        async with semaphore:
            async with aiohttp.ClientSession() as session:
                tasks = [self.convert(session, link) for link in self.links]
                return await asyncio.gather(*tasks)

    async def compile(self):
        return await self.fetch()

    def run(self):
        loop = asyncio.get_event_loop()
        data = loop.run_until_complete(self.compile())

        self.content.extend(data)
        return data

    def create_request_links(self) -> None:
        """Paginates through the first page of an FPDS response and creates a
        list of all pagination links. This method will not have a return; it
        will set the `links` attributes to an iterable of strings.
        """
        self.initial_request()
        params = self.search_params
        tree = fpdsXML(content=self.content[0])
        links = tree.pagination_links(params=params)
        if len(links) > 1:
            links.pop(0)  # don't need to make initial request a second time
        self.links = links

    def process_tree(self, tree):
        """Process a single tree and return the records"""
        xml = fpdsXML(content=tree)
        records = xml.jsonified_entries()
        return records

    @staticmethod
    def process_xml_and_jsonify(entry):
        # Process the fpdsXML entry and call jsonified_entries
        print(f"processing {entry.__str__()}")
        return entry.jsonified_entries()

    # @timeit
    def join_records(self):
        num_processes = multiprocessing.cpu_count()
        data = self.run()

        print("Finished retrieving data")
        # Use a multiprocessing pool for parallel processing
        with multiprocessing.Pool(processes=num_processes) as pool:
            results = pool.map(self.process_xml_and_jsonify, data)

        return results
