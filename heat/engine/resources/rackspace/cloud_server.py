#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import tempfile

import json
import paramiko
from Crypto.PublicKey import RSA
import novaclient.exceptions as novaexception

from heat.common import exception
from heat.openstack.common import log as logging
from heat.engine import scheduler
from heat.engine.resources import instance
from heat.engine.resources.rackspace import rackspace_resource
from heat.db.sqlalchemy import api as db_api

logger = logging.getLogger(__name__)


class CloudServer(instance.Instance):
    """Resource for Rackspace Cloud Servers."""

    properties_schema = {'ServerName': {'Type': 'String', 'Required': True},
                         'Flavor': {'Type': 'String', 'Required': True},
                         'ImageName': {'Type': 'String', 'Required': True},
                         'UserData': {'Type': 'String'},
                         'PublicKey': {'Type': 'String'},
                         'Volumes': {'Type': 'List'}}

    attributes_schema = {'PrivateDnsName': ('Private DNS name of the specified'
                                            ' instance.'),
                         'PublicDnsName': ('Public DNS name of the specified '
                                           'instance.'),
                         'PrivateIp': ('Private IP address of the specified '
                                       'instance.'),
                         'PublicIp': ('Public IP address of the specified '
                                      'instance.')}

    base_script = """#!/bin/bash

# Install cloud-init and heat-cfntools
%s
# Create data source for cloud-init
mkdir -p /var/lib/cloud/seed/nocloud-net
mv /tmp/userdata /var/lib/cloud/seed/nocloud-net/user-data
touch /var/lib/cloud/seed/nocloud-net/meta-data
chmod 600 /var/lib/cloud/seed/nocloud-net/*

# Run cloud-init & cfn-init
cloud-init start
bash -x /var/lib/cloud/data/cfn-userdata > /root/cfn-userdata.log 2>&1
"""

    ubuntu_script = base_script % """\
apt-get update
apt-get install -y cloud-init python-boto python-pip gcc python-dev
pip install heat-cfntools
"""

    fedora_script = base_script % """\
yum install -y cloud-init python-boto python-pip gcc python-devel
pip-python install heat-cfntools
"""

    # TODO(jason): Install cloud-init & other deps from third-party repos
    centos_script = base_script % """\
yum install -y cloud-init python-boto python-pip gcc python-devel
pip-python install heat-cfntools
"""

    # TODO(jason): Install cloud-init & other deps from third-party repos
    arch_script = base_script % """\
pacman -S --noconfirm python-pip gcc
"""

    # TODO(jason): Install cloud-init & other deps from third-party repos
    gentoo_script = base_script % """\
emerge cloud-init python-boto python-pip gcc python-devel
"""

    # TODO(jason): Install cloud-init & other deps from third-party repos
    opensuse_script = base_script % """\
zypper --non-interactive rm patterns-openSUSE-minimal_base-conflicts
zypper --non-interactive in cloud-init python-boto python-pip gcc python-devel
"""

    # List of supported Linux distros and their corresponding config scripts
    image_scripts = {'arch': None,
                     'centos': None,
                     'debian': None,
                     'fedora': fedora_script,
                     'gentoo': None,
                     'opensuse': None,
                     'rhel': None,
                     'ubuntu': ubuntu_script}

    # Cache data retrieved from APIs in class attributes
    _image_id_map = {}
    _distro_map = {}
    _server_map = {}

    # Template keys supported for handle_update.  Properties not
    # listed here trigger an UpdateReplace
    update_allowed_keys = ('Metadata', 'Properties')
    update_allowed_properties = ('Flavor', 'ServerName')

    def __init__(self, name, json_snippet, stack):
        super(CloudServer, self).__init__(name, json_snippet, stack)
        self._private_key = None
        self.rs = rackspace_resource.RackspaceResource(name,
                                                       json_snippet,
                                                       stack)

    def nova(self):
        return self.rs.nova()  # Override the Instance method

    def cinder(self):
        return self.rs.cinder()

    @property
    def server(self):
        """Get the Cloud Server object."""
        if self.resource_id in self.__class__._server_map:
            return self.__class__._server_map[self.resource_id]
        else:
            server = self.nova().servers.get(self.resource_id)
            self.__class__._server_map[self.resource_id] = server
            return server

    @property
    def image_id(self):
        """Get the image ID corresponding to the ImageName property."""
        image_name = self.properties['ImageName']
        if image_name in self.__class__._image_id_map:
            return self.__class__._image_id_map[image_name]
        else:
            image_id = self._get_image_id(image_name)
            self.__class__._image_id_map[image_name] = image_id
            return image_id

    @property
    def distro(self):
        """Get the Linux distribution for this server."""
        if self.image_id in self.__class__._distro_map:
            return self.__class__._distro_map[self.image_id]
        else:
            image = self.nova().images.get(self.image_id)
            distro = image.metadata['os_distro']
            self.__class__._distro_map[self.image_id] = distro
            return distro

    @property
    def script(self):
        """Get the config script for the Cloud Server image."""
        return self.image_scripts[self.distro]

    @property
    def flavors(self):
        """Get the flavors from the API or cache (updated every 6 hours)."""
        return [flavor.id for flavor in self.nova().flavors.list()]

    @property
    def private_key(self):
        """Return the private SSH key for the resource."""
        if self._private_key:
            return self._private_key
        if self.id is not None:
            private_key = db_api.resource_data_get(self, 'private_key')
            if not private_key:
                return None
            self._private_key = private_key
            return private_key

    @private_key.setter
    def private_key(self, private_key):
        """Save the resource's private SSH key to the database."""
        self._private_key = private_key
        if self.id is not None:
            db_api.resource_data_set(self, 'private_key', private_key, True)

    def _get_ip(self, ip_type):
        """Return the IP of the Cloud Server."""
        def ip_not_found():
            exc = exception.Error("Could not determine the %s IP of %s." %
                                  (ip_type, self.properties['ImageName']))
            raise exception.ResourceFailure(exc)

        if ip_type not in self.server.addresses:
            ip_not_found()
        for ip in self.server.addresses[ip_type]:
            if ip['version'] == 4:
                return ip['addr']
        ip_not_found()

    @property
    def public_ip(self):
        """Return the public IP of the Cloud Server."""
        return self._get_ip('public')

    @property
    def private_ip(self):
        """Return the private IP of the Cloud Server."""
        try:
            return self._get_ip('private')
        except exception.ResourceFailure as ex:
            logger.info(ex.message)

    def validate(self):
        """Validate user parameters."""
        if self.properties['Flavor'] not in self.flavors:
            return {'Error': "Flavor not found."}
        if not self.script:
            return {'Error': "Image %s not supported." %
                    self.properties['ImageName']}

    def _run_ssh_command(self, command):
        """Run a shell command on the Cloud Server via SSH."""
        with tempfile.NamedTemporaryFile() as private_key_file:
            private_key_file.write(self.private_key)
            private_key_file.seek(0)
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.MissingHostKeyPolicy())
            ssh.connect(self.public_ip,
                        username="root",
                        key_filename=private_key_file.name)
            stdin, stdout, stderr = ssh.exec_command(command)
            logger.debug(stdout.read())
            logger.debug(stderr.read())

    def _sftp_files(self, files):
        """Transfer files to the Cloud Server via SFTP."""
        with tempfile.NamedTemporaryFile() as private_key_file:
            private_key_file.write(self.private_key)
            private_key_file.seek(0)
            pkey = paramiko.RSAKey.from_private_key_file(private_key_file.name)
            transport = paramiko.Transport((self.public_ip, 22))
            transport.connect(hostkey=None, username="root", pkey=pkey)
            sftp = paramiko.SFTPClient.from_transport(transport)
            for remote_file in files:
                sftp_file = sftp.open(remote_file['path'], 'w')
                sftp_file.write(remote_file['data'])
                sftp_file.close()

    def handle_create(self):
        """Create a Rackspace Cloud Servers container.

        Rackspace Cloud Servers does not have the metadata service
        running, so we have to transfer the user-data file to the
        server and then trigger cloud-init.
        """
        # Retrieve server creation parameters from properties
        flavor = self.properties['Flavor']
        user_public_key = self.properties['PublicKey'] or ''

        # Generate SSH public/private keypair
        rsa = RSA.generate(1024)
        self.private_key = rsa.exportKey()
        public_key = rsa.publickey().exportKey('OpenSSH')
        public_keys = public_key + "\n" + user_public_key
        personality_files = {"/root/.ssh/authorized_keys": public_keys}

        # Create server
        client = self.nova().servers
        server = client.create(self.properties['ServerName'],
                               self.image_id,
                               flavor,
                               files=personality_files)

        # Save resource ID to db
        self.resource_id_set(server.id)

        return server, scheduler.TaskRunner(self._attach_volumes_task())

    def _attach_volumes_task(self):
        tasks = (scheduler.TaskRunner(self._attach_volume, volume_id, device)
                 for volume_id, device in self.volumes())
        return scheduler.PollingTaskGroup(tasks)

    def _attach_volume(self, volume_id, device):
        self.nova().volumes.create_server_volume(self.server.id,
                                                 volume_id,
                                                 device or None)
        yield
        volume = self.cinder().get(volume_id)
        while volume.status in ('available', 'attaching'):
            yield
            volume.get()

        if volume.status != 'in-use':
            raise exception.Error(volume.status)

    def _detach_volumes_task(self):
        tasks = (scheduler.TaskRunner(self._detach_volume, volume_id)
                 for volume_id, device in self.volumes())
        return scheduler.PollingTaskGroup(tasks)

    def _detach_volume(self, volume_id):
        volume = self.cinder().get(volume_id)
        volume.detach()
        yield
        while volume.status in ('in-use', 'detaching'):
            yield
            volume.get()

        if volume.status != 'available':
            raise exception.Error(volume.status)

    def check_create_complete(self, cookie):
        """Check if server creation is complete and handle server configs."""
        if not self._check_active(cookie):
            return False

        # Create heat-script and userdata files on server
        raw_userdata = self.properties['UserData'] or ''
        userdata = self._build_userdata(raw_userdata)
        files = [{'path': "/tmp/userdata", 'data': userdata},
                 {'path': "/root/heat-script.sh", 'data': self.script}]
        self._sftp_files(files)

        # Connect via SSH and run script
        command = "bash -ex /root/heat-script.sh > /root/heat-script.log 2>&1"
        self._run_ssh_command(command)

        return True

    # TODO(jason): Make this consistent with Instance and inherit
    def _delete_server(self, server):
        """Return a coroutine that deletes the Cloud Server."""
        server.delete()
        while True:
            yield
            try:
                server.get()
                if server.status == "ERROR":
                    exc = exception.Error("Deletion of server %s failed." %
                                          server.name)
                    raise exception.ResourceFailure(exc)
            except novaexception.NotFound:
                break

    def handle_update(self, json_snippet, tmpl_diff, prop_diff):
        """Try to update a Cloud Server's parameters.

        If the Cloud Server's Metadata or Flavor changed, update the
        Cloud Server.  If any other parameters changed, re-create the
        Cloud Server with the new parameters.
        """
        if 'Metadata' in tmpl_diff:
            self.metadata = json_snippet['Metadata']
            metadata_string = json.dumps(self.metadata)

            files = [{'path': "/var/cache/heat-cfntools/last_metadata",
                      'data': metadata_string}]
            self._sftp_files(files)

            command = "bash -x /var/lib/cloud/data/cfn-userdata > " + \
                      "/root/cfn-userdata.log 2>&1"
            self._run_ssh_command(command)

        if 'Flavor' in prop_diff:
            self.flavor = json_snippet['Properties']['Flavor']
            self.server.resize(self.flavor)
            resize = scheduler.TaskRunner(self._check_resize,
                                          self.server,
                                          self.flavor)
            resize(wait_time=1.0)

        # If ServerName is the only update, fail update
        if prop_diff.keys() == ['ServerName'] and \
           tmpl_diff.keys() == ['Properties']:
            raise exception.NotSupported(feature="Cloud Server rename")
        # Other updates were successful, so don't cause update to fail
        elif 'ServerName' in prop_diff:
            logger.info("Cloud Server rename not supported.")

        return True

    def _resolve_attribute(self, key):
        """Return the method that provides a given template attribute."""
        attribute_function = {'PublicIp': self.public_ip,
                              'PrivateIp': self.private_ip,
                              'PublicDnsName': self.public_ip,
                              'PrivateDnsName': self.public_ip}
        if key not in attribute_function:
            raise exception.InvalidTemplateAttribute(resource=self.name,
                                                     key=key)
        function = attribute_function[key]
        logger.info('%s._resolve_attribute(%s) == %s'
                    % (self.name, key, function))
        return unicode(function)


# pyrax module is required to work with Rackspace cloud server provider.
# If it is not installed, don't register cloud server provider
def resource_mapping():
    if rackspace_resource.PYRAX_INSTALLED:
        return {'Rackspace::Cloud::Server': CloudServer}
    else:
        return {}
