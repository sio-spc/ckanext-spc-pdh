# -*- coding: utf-8 -*-
import requests
import traceback
from bs4 import BeautifulSoup

from ckan.lib.helpers import json
from ckan.lib.munge import munge_tag
from ckanext.harvest.model import HarvestObject
from ckanext.harvest.harvesters import HarvesterBase
from ckan.logic import get_action
from pylons import config

import logging
log = logging.getLogger(__name__)


class SpcDotStatHarvester(HarvesterBase):
    HARVEST_USER = 'harvest'

    ACCESS_TYPES = {
        '': '',
        'direct_access': 1,
        'public_use': 2,
        'licensed': 3,
        'data_enclave': 4,
        'data_external': 5,
        'no_data_available': 6,
        'open_data': 7,
    }


    def info(self):
        return {
            'name': 'dotstat',
            'title': '.Stat harvester for SDMX',
            'description': (
                'Harvests SDMX data from a .Stat instance '
            ),
            'form_config_interface': 'Text'
        }

    def _set_config(self, config_str):
        if config_str:
            self.config = json.loads(config_str)
        else:
            self.config = {}

        if 'user' not in self.config:
            self.config['user'] = self.HARVEST_USER

        log.debug('Using config: %r' % self.config)
    
    def get_endpoints(self, base_url):
        '''
        Finds all Dataflows under SPC
        Goes to dataflow endpoint and parses SDMX, finding dataflow IDs
        Returns a list of strings
        '''
        endpoints = []
        resources_url = base_url + 'dataflow/SPC/all'
        resp = requests.get(resources_url)
        #print(resp.text)
        soup = BeautifulSoup(resp.text, 'xml')
        #print(soup)
        for name in soup.findAll('Dataflow'):
            endpoints.append(name['id'])
        return endpoints

    def gather_stage(self, harvest_job):
        log.debug('In DotStatHarvester gather_stage')

        # For each row of data, use its ID as the GUID and save a harvest object
        # Return a list of all these new harvest jobs
        try:
            harvest_obj_ids = []
            self._set_config(harvest_job.source.config)
            base_url = harvest_job.source.url

            try:
                # Get list of endpoint ids
                endpoints = self.get_endpoints(base_url)

            except (AccessTypeNotAvailableError, KeyError):
                log.debug('Endpoint function failed')
                
            # Make a harvest object for each dataset
            # Set the GUID to the dataset's ID (DF_SDG etc.)
            for i, end in enumerate(endpoints):
                harvest_obj = HarvestObject(
                    guid=end,
                    job=harvest_job
                )
                harvest_obj.save()
                harvest_obj_ids.append(harvest_obj.id)

            log.debug('IDs: %r' % harvest_obj_ids)

            return harvest_obj_ids
        
        except Exception, e:
            self._save_gather_error(
                'Unable to get content for URL: %s: %s / %s'
                % (base_url, str(e), traceback.format_exc()),
                harvest_job
            )

    # Get the SDMX formatted resource for the GUID
    # Put this in harvest_object's 'content' as text
    def fetch_stage(self, harvest_object):
        log.debug('In DotStatHarvester fetch_stage')
        self._set_config(harvest_object.job.source.config)

        if not harvest_object:
            log.error('No harvest object received')
            self._save_object_error(
                'No harvest object received',
                harvest_object
            )
            return False

        base_url = harvest_object.source.url
        # Build the url where we'll fetch basic metadata
        meta_suffix = '1.0/?references=all&detail=referencepartial'
        metadata_url = '{}dataflow/SPC/{}/{}'.format(base_url, harvest_object.guid, meta_suffix)

        try:
            log.debug('Fetching content from %s' % metadata_url)
            meta = requests.get(metadata_url)
            meta.encoding = 'utf-8'
            # Dump page contents into harvest object content
            harvest_object.content = meta.text
            harvest_object.save()
            log.debug('successfully processed ' + harvest_object.guid)
            return True

        except Exception, e:
            self._save_object_error(
                (
                    'Unable to get content for package: %s: %r / %s'
                    % (metadata_url, e, traceback.format_exc())
                ),
                harvest_object
            )
            return False
    
    # Parse the SDMX text and assign to correct fields of package dict
    def import_stage(self, harvest_object):
        log.debug('In DotStatHarvester import_stage')
        self._set_config(harvest_object.job.source.config)

        if not harvest_object:
            log.error('No harvest object received')
            self._save_object_error(
                'No harvest object received',
                harvest_object
            )
            return False

        try:
            base_url = harvest_object.source.url
            # Parse the SDMX as XML with bs4
            soup = BeautifulSoup(harvest_object.content, 'xml')

            # Make a package dict
            pkg_dict = {}
            pkg_dict['id'] = harvest_object.guid

            # Added thematic string
            pkg_dict['thematic_area_string'] = ["Statistics"]
            
            # Get owner_org if there is one
            source_dataset = get_action('package_show')({
                'ignore_auth': True
            }, {
                'id': harvest_object.source.id
            })
            owner_org = source_dataset.get('owner_org')
            pkg_dict['owner_org'] = owner_org

            # Match other fields with tags in XML structure
            structure = soup.find('Dataflow', attrs= {'id': pkg_dict['id']})
            pkg_dict['title'] = structure.find('Name').text
            pkg_dict['publisher_name'] = structure['agencyID']
            pkg_dict['version'] = structure['version']
            
            # Need to change url to point to Data Explorer
            de_url = 'https://stats.pacificdata.org/data-explorer/#/vis?locale=en&endpointId=disseminate&agencyId=SPC&code={}&version=1.0&viewerId=table&data=.&startPeriod=2005&endPeriod=2018'.format(harvest_object.guid)
            pkg_dict['source'] = de_url

            # Set a default resource
            pkg_dict['resources'] = [{'url': 'https://stats.pacificdata.org/data-nsi/Rest/data/SPC,{},1.0/all/?format=csv'.format(harvest_object.guid),
                                    'format': 'CSV',
                                    'mimetype': 'CSV',
                                    'description': 'All data for {}'.format(pkg_dict['title']),
                                    'name': '{} Data CSV'.format(pkg_dict['title'])}]

            # Get notes/description if it exists
            try:
                pkg_dict['notes'] = structure.find('Description').text
            except:  
                pkg_dict['notes'] = ''
            
            '''
            May need modifying when DF_SDG is broken into several DFs
            This gets the list of indicators for SDG-related dataflows
            Stores the list of strings in 'alternate_identifier' field
            '''
            if soup.find('Codelist', attrs={'id': 'CL_SDG_SERIES'}) is not None:
                pkg_dict['alternate_identifier'] = []
                codelist = soup.find('Codelist', attrs={'id': 'CL_SDG_SERIES'})
                for indic in codelist.findAll('Name'):
                    if indic.text == 'SDG Indicator or Series':
                        continue
                    pkg_dict['alternate_identifier'].append(indic.text)

            '''
            When support for metadata endpoints arrives in .Stat, here is how more metadata may be found:
            # Use the metadata/flow endpoint
            metadata = requests.get('{}metadata/data/{}/all?detail=full'.format(base_url, harvest_object.guid))
            
            # Parse with bs4
            parsed = BeautifulSoup(metadata.text, 'xml')
            
            # Now search for tags which may be useful as metadata
            # example: getting the name and definition of metadata set
            # (may need tweaking depending on SPC's metadata setup)
            
            # We can get name from the metadata structure
            set = parsed.find('MetadataSet')
            pkg_dict['name'] = set.find('Name').text
            
            # Then we can go to the reported attribute structure for more details
            detail = set.find('ReportedAttribute', attrs={'id': 'DEF'})
            pkg_dict['notes'] = detail.find('StructuredText', attrs={'lang': 'en'})
            source_details = set.find('ReportedAttribute', attrs={'id': 'SOURCE_DEF'})
            pkg_dict['source'] = source_details.find('StructuredText', attrs={'lang': 'en'})
            '''

            log.debug('package dict: %s' % pkg_dict)

            # Now create the package
            return self._create_or_update_package(pkg_dict, harvest_object,
                package_dict_form='package_show')
        except Exception, e:
            self._save_object_error(
                (
                    'Exception in import stage: %r / %s'
                    % (e, traceback.format_exc())
                ),
                harvest_object
            )
            return False

class AccessTypeNotAvailableError(Exception):
    pass
