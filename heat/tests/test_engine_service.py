# vim: tabstop=4 shiftwidth=4 softtabstop=4

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


import functools
import json
import sys

import mox
from testtools import matchers

from oslo.config import cfg

from heat.engine import environment
from heat.common import exception
from heat.tests.v1_1 import fakes
import heat.rpc.api as engine_api
import heat.db.api as db_api
from heat.common import identifier
from heat.common import template_format
from heat.engine import parser
from heat.engine import service
from heat.engine.properties import Properties
from heat.engine.resources import instance as instances
from heat.engine import watchrule
from heat.openstack.common import threadgroup
from heat.tests.common import HeatTestCase
from heat.tests import utils
from heat.tests.utils import dummy_context
from heat.tests.utils import setup_dummy_db


wp_template = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "WordPress",
  "Parameters" : {
    "KeyName" : {
      "Description" : "KeyName",
      "Type" : "String",
      "Default" : "test"
    }
  },
  "Resources" : {
    "WebServer": {
      "Type": "AWS::EC2::Instance",
      "Properties": {
        "ImageId" : "F17-x86_64-gold",
        "InstanceType"   : "m1.large",
        "KeyName"        : "test",
        "UserData"       : "wordpress"
      }
    }
  }
}
'''

alarm_template = '''
{
  "AWSTemplateFormatVersion" : "2010-09-09",
  "Description" : "alarming",
  "Resources" : {
    "service_alarm": {
      "Type": "AWS::CloudWatch::Alarm",
      "Properties": {
        "EvaluationPeriods": "1",
        "AlarmActions": [],
        "AlarmDescription": "do the thing",
        "Namespace": "dev/null",
        "Period": "300",
        "ComparisonOperator": "GreaterThanThreshold",
        "Statistic": "SampleCount",
        "Threshold": "2",
        "MetricName": "ServiceFailure"
      }
    }
  }
}
'''


def create_context(mocks, user='stacks_test_user',
                   tenant_id='test_admin', password='stacks_test_password'):
    ctx = dummy_context()
    mocks.StubOutWithMock(ctx, 'username')
    mocks.StubOutWithMock(ctx, 'tenant_id')
    mocks.StubOutWithMock(ctx, 'password')
    ctx.username = user
    ctx.tenant_id = tenant_id
    ctx.password = password
    return ctx


def get_wordpress_stack(stack_name, ctx):
    t = template_format.parse(wp_template)
    template = parser.Template(t)
    stack = parser.Stack(ctx, stack_name, template,
                         environment.Environment({'KeyName': 'test'}))
    return stack


def get_alarm_stack(stack_name, ctx):
    t = template_format.parse(alarm_template)
    template = parser.Template(t)
    stack = parser.Stack(ctx, stack_name, template)
    return stack


def setup_mocks(mocks, stack):
    fc = fakes.FakeClient()
    mocks.StubOutWithMock(instances.Instance, 'nova')
    instances.Instance.nova().MultipleTimes().AndReturn(fc)

    instance = stack.resources['WebServer']
    server_userdata = instance._build_userdata(instance.properties['UserData'])
    mocks.StubOutWithMock(fc.servers, 'create')
    fc.servers.create(image=744, flavor=3, key_name='test',
                      name=utils.PhysName(stack.name, 'WebServer'),
                      security_groups=None,
                      userdata=server_userdata, scheduler_hints=None,
                      meta=None, nics=None,
                      availability_zone=None).AndReturn(
                          fc.servers.list()[-1])
    return fc


def setup_stack(stack_name, ctx, create_res=True):
    stack = get_wordpress_stack(stack_name, ctx)
    stack.store()
    if create_res:
        m = mox.Mox()
        setup_mocks(m, stack)
        m.ReplayAll()
        stack.create()
        m.UnsetStubs()
    return stack


def clean_up_stack(stack, delete_res=True):
    if delete_res:
        m = mox.Mox()
        fc = setup_mocks(m, stack)
        m.StubOutWithMock(fc.client, 'get_servers_9999')
        get = fc.client.get_servers_9999
        get().AndRaise(service.clients.novaclient.exceptions.NotFound(404))
        m.ReplayAll()
    stack.delete()
    if delete_res:
        m.UnsetStubs()


def stack_context(stack_name, create_res=True):
    """
    Decorator which creates a stack by using the test case's context and
    deletes it afterwards to ensure tests clean up their stacks regardless
    of test success/failure
    """
    def stack_delete(test_fn):
        @functools.wraps(test_fn)
        def wrapped_test(test_case, *args, **kwargs):
            def create_stack():
                ctx = getattr(test_case, 'ctx', None)
                if ctx is not None:
                    stack = setup_stack(stack_name, ctx, create_res)
                    setattr(test_case, 'stack', stack)

            def delete_stack():
                stack = getattr(test_case, 'stack', None)
                if stack is not None and stack.id is not None:
                    clean_up_stack(stack, delete_res=create_res)

            create_stack()
            try:
                test_fn(test_case, *args, **kwargs)
            except:
                exc_class, exc_val, exc_tb = sys.exc_info()
                try:
                    delete_stack()
                finally:
                    raise exc_class, exc_val, exc_tb
            else:
                delete_stack()

        return wrapped_test
    return stack_delete


class DummyThreadGroup(object):
    def __init__(self):
        self.threads = []

    def add_timer(self, interval, callback, initial_delay=None,
                  *args, **kwargs):
        self.threads.append(callback)

    def add_thread(self, callback, *args, **kwargs):
        self.threads.append(callback)

    def stop(self):
        pass

    def wait(self):
        pass


class StackCreateTest(HeatTestCase):
    def setUp(self):
        super(StackCreateTest, self).setUp()
        setup_dummy_db()

    def test_wordpress_single_instance_stack_create(self):
        stack = get_wordpress_stack('test_stack', create_context(self.m))
        setup_mocks(self.m, stack)
        self.m.ReplayAll()
        stack.store()
        stack.create()

        self.assertNotEqual(stack.resources['WebServer'], None)
        self.assertTrue(stack.resources['WebServer'].resource_id > 0)
        self.assertNotEqual(stack.resources['WebServer'].ipaddress, '0.0.0.0')

    def test_wordpress_single_instance_stack_delete(self):
        ctx = create_context(self.m)
        stack = get_wordpress_stack('test_stack', ctx)
        fc = setup_mocks(self.m, stack)
        self.m.ReplayAll()
        stack_id = stack.store()
        stack.create()

        db_s = db_api.stack_get(ctx, stack_id)
        self.assertNotEqual(db_s, None)

        self.assertNotEqual(stack.resources['WebServer'], None)
        self.assertTrue(stack.resources['WebServer'].resource_id > 0)

        self.m.StubOutWithMock(fc.client, 'get_servers_9999')
        get = fc.client.get_servers_9999
        get().AndRaise(service.clients.novaclient.exceptions.NotFound(404))
        mox.Replay(get)
        stack.delete()

        rsrc = stack.resources['WebServer']
        self.assertEqual((rsrc.DELETE, rsrc.COMPLETE), rsrc.state)
        self.assertEqual((stack.DELETE, stack.COMPLETE), rsrc.state)
        self.assertEqual(None, db_api.stack_get(ctx, stack_id))
        self.assertEqual('DELETE', db_s.action)
        self.assertEqual('COMPLETE', db_s.status, )


class StackServiceCreateUpdateDeleteTest(HeatTestCase):

    def setUp(self):
        super(StackServiceCreateUpdateDeleteTest, self).setUp()
        setup_dummy_db()
        self.ctx = create_context(self.m)

        self.man = service.EngineService('a-host', 'a-topic')

    def test_stack_create(self):
        stack_name = 'service_create_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(parser, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        parser.Template(template, files=None).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t, stack.env).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(DummyThreadGroup())

        self.m.ReplayAll()

        result = self.man.create_stack(self.ctx, stack_name,
                                       template, params, None, {})
        self.assertEqual(stack.identifier(), result)
        self.assertTrue(isinstance(result, dict))
        self.assertTrue(result['stack_id'])
        self.m.VerifyAll()

    def test_stack_create_verify_err(self):
        stack_name = 'service_create_verify_err_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(parser, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')
        self.m.StubOutWithMock(parser, 'Stack')

        parser.Template(template, files=None).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t,
                     stack.env).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndRaise(exception.StackValidationFailed(
            message='fubar'))

        self.m.ReplayAll()

        self.assertRaises(
            exception.StackValidationFailed,
            self.man.create_stack,
            self.ctx, stack_name,
            template, params, None, {})
        self.m.VerifyAll()

    def test_stack_create_invalid_stack_name(self):
        stack_name = 'service_create/test_stack'
        stack = get_wordpress_stack('test_stack', self.ctx)

        self.assertRaises(ValueError,
                          self.man.create_stack,
                          self.ctx, stack_name, stack.t, {}, None, {})

    def test_stack_create_invalid_resource_name(self):
        stack_name = 'service_create_test_stack_invalid_res'
        stack = get_wordpress_stack(stack_name, self.ctx)
        tmpl = dict(stack.t)
        tmpl['Resources']['Web/Server'] = tmpl['Resources']['WebServer']
        del tmpl['Resources']['WebServer']

        self.assertRaises(ValueError,
                          self.man.create_stack,
                          self.ctx, stack_name,
                          stack.t, {}, None, {})

    def test_stack_create_no_credentials(self):
        stack_name = 'service_create_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        ctx = self.ctx = create_context(self.m, password=None)
        self.assertRaises(exception.MissingCredentialError,
                          self.man.create_stack, ctx, stack_name, template,
                          params, None, {})

        ctx = self.ctx = create_context(self.m, user=None)
        self.assertRaises(exception.MissingCredentialError,
                          self.man.create_stack, ctx, stack_name, template,
                          params, None, {})

    def test_stack_validate(self):
        stack_name = 'service_create_test_validate'
        stack = get_wordpress_stack(stack_name, self.ctx)
        setup_mocks(self.m, stack)

        template = dict(stack.t)
        template['Parameters']['KeyName']['Default'] = 'test'
        resource = stack['WebServer']

        self.m.ReplayAll()

        resource.properties = Properties(
            resource.properties_schema,
            {
                'ImageId': 'CentOS 5.2',
                'KeyName': 'test',
                'InstanceType': 'm1.large'
            })
        stack.validate()

        resource.properties = Properties(
            resource.properties_schema,
            {
                'KeyName': 'test',
                'InstanceType': 'm1.large'
            })
        self.assertRaises(exception.StackValidationFailed, stack.validate)

    def test_stack_delete(self):
        stack_name = 'service_delete_test_stack'
        stack = get_wordpress_stack(stack_name, self.ctx)
        sid = stack.store()

        s = db_api.stack_get(self.ctx, sid)
        self.m.StubOutWithMock(parser.Stack, 'load')

        parser.Stack.load(self.ctx, stack=s).AndReturn(stack)

        self.man.tg = DummyThreadGroup()

        self.m.ReplayAll()

        self.assertEqual(None,
                         self.man.delete_stack(self.ctx, stack.identifier()))
        self.m.VerifyAll()

    def test_stack_delete_nonexist(self):
        stack_name = 'service_delete_nonexist_test_stack'
        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.man.delete_stack,
                          self.ctx, stack.identifier())
        self.m.VerifyAll()

    def test_stack_update(self):
        stack_name = 'service_update_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        old_stack = get_wordpress_stack(stack_name, self.ctx)
        sid = old_stack.store()
        s = db_api.stack_get(self.ctx, sid)

        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(parser, 'Stack')
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=s).AndReturn(old_stack)

        self.m.StubOutWithMock(parser, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')

        parser.Template(template, files=None).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t, stack.env).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndReturn(None)

        self.m.StubOutWithMock(threadgroup, 'ThreadGroup')
        threadgroup.ThreadGroup().AndReturn(DummyThreadGroup())

        self.m.ReplayAll()

        result = self.man.update_stack(self.ctx, old_stack.identifier(),
                                       template, params, None, {})
        self.assertEqual(old_stack.identifier(), result)
        self.assertTrue(isinstance(result, dict))
        self.assertTrue(result['stack_id'])
        self.m.VerifyAll()

    def test_stack_update_verify_err(self):
        stack_name = 'service_update_verify_err_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        old_stack = get_wordpress_stack(stack_name, self.ctx)
        old_stack.store()
        sid = old_stack.store()
        s = db_api.stack_get(self.ctx, sid)

        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.StubOutWithMock(parser, 'Stack')
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=s).AndReturn(old_stack)

        self.m.StubOutWithMock(parser, 'Template')
        self.m.StubOutWithMock(environment, 'Environment')

        parser.Template(template, files=None).AndReturn(stack.t)
        environment.Environment(params).AndReturn(stack.env)
        parser.Stack(self.ctx, stack.name,
                     stack.t, stack.env).AndReturn(stack)

        self.m.StubOutWithMock(stack, 'validate')
        stack.validate().AndRaise(exception.StackValidationFailed(
            message='fubar'))

        self.m.ReplayAll()

        self.assertRaises(
            exception.StackValidationFailed,
            self.man.update_stack,
            self.ctx, old_stack.identifier(),
            template, params, None, {})
        self.m.VerifyAll()

    def test_stack_update_nonexist(self):
        stack_name = 'service_update_nonexist_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'
        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.man.update_stack,
                          self.ctx, stack.identifier(), template, params,
                          None, {})
        self.m.VerifyAll()

    def test_stack_update_no_credentials(self):
        stack_name = 'service_update_nonexist_test_stack'
        params = {'foo': 'bar'}
        template = '{ "Template": "data" }'

        stack = get_wordpress_stack(stack_name, self.ctx)

        ctx = self.ctx = create_context(self.m, password=None)
        self.assertRaises(exception.MissingCredentialError,
                          self.man.update_stack,
                          ctx, stack.identifier(), template, params,
                          None, {})

        ctx = self.ctx = create_context(self.m, user=None)
        self.assertRaises(exception.MissingCredentialError,
                          self.man.update_stack,
                          ctx, stack.identifier(), template, params,
                          None, {})


class StackServiceSuspendResumeTest(HeatTestCase):

    def setUp(self):
        super(StackServiceSuspendResumeTest, self).setUp()
        setup_dummy_db()
        self.ctx = create_context(self.m)

        self.man = service.EngineService('a-host', 'a-topic')

    def test_stack_suspend(self):
        stack_name = 'service_suspend_test_stack'
        stack = get_wordpress_stack(stack_name, self.ctx)
        sid = stack.store()
        s = db_api.stack_get(self.ctx, sid)

        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=s).AndReturn(stack)

        self.m.StubOutWithMock(service.EngineService, '_start_in_thread')
        service.EngineService._start_in_thread(sid,
                                               mox.IgnoreArg(),
                                               stack).AndReturn(None)
        self.m.ReplayAll()

        result = self.man.stack_suspend(self.ctx, stack.identifier())
        self.assertEqual(None, result)

        self.m.VerifyAll()

    @stack_context('service_resume_test_stack', False)
    def test_stack_resume(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.StubOutWithMock(service.EngineService, '_start_in_thread')
        service.EngineService._start_in_thread(self.stack.id,
                                               mox.IgnoreArg(),
                                               self.stack).AndReturn(None)
        self.m.ReplayAll()

        result = self.man.stack_resume(self.ctx, self.stack.identifier())
        self.assertEqual(None, result)
        self.m.VerifyAll()

    def test_stack_suspend_nonexist(self):
        stack_name = 'service_suspend_nonexist_test_stack'
        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.man.stack_suspend, self.ctx, stack.identifier())
        self.m.VerifyAll()

    def test_stack_resume_nonexist(self):
        stack_name = 'service_resume_nonexist_test_stack'
        stack = get_wordpress_stack(stack_name, self.ctx)

        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.man.stack_resume, self.ctx, stack.identifier())
        self.m.VerifyAll()


class StackServiceTest(HeatTestCase):

    def setUp(self):
        super(StackServiceTest, self).setUp()

        self.ctx = create_context(self.m,
                                  tenant_id='stack_service_test_tenant')
        self.eng = service.EngineService('a-host', 'a-topic')
        cfg.CONF.set_default('heat_stack_user_role', 'stack_user_role')

        setup_dummy_db()

    @stack_context('service_identify_test_stack', False)
    def test_stack_identify(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        identity = self.eng.identify_stack(self.ctx, self.stack.name)
        self.assertEqual(self.stack.identifier(), identity)

        self.m.VerifyAll()

    @stack_context('service_identify_uuid_test_stack', False)
    def test_stack_identify_uuid(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        identity = self.eng.identify_stack(self.ctx, self.stack.id)
        self.assertEqual(self.stack.identifier(), identity)

        self.m.VerifyAll()

    def test_stack_identify_nonexist(self):
        self.assertRaises(exception.StackNotFound, self.eng.identify_stack,
                          self.ctx, 'wibble')

    @stack_context('service_create_existing_test_stack', False)
    def test_stack_create_existing(self):
        self.assertRaises(exception.StackExists, self.eng.create_stack,
                          self.ctx, self.stack.name, self.stack.t, {},
                          None, {})

    @stack_context('service_name_tenants_test_stack', False)
    def test_stack_by_name_tenants(self):
        self.assertEqual(self.stack.id,
                         db_api.stack_get_by_name(self.ctx,
                                                  self.stack.name).id)
        ctx2 = create_context(self.m, tenant_id='stack_service_test_tenant2')
        self.assertEqual(None, db_api.stack_get_by_name(ctx2, self.stack.name))

    @stack_context('service_event_list_test_stack')
    def test_stack_event_list(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = db_api.stack_get(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.ReplayAll()

        events = self.eng.list_events(self.ctx, self.stack.identifier())

        self.assertEqual(2, len(events))
        for ev in events:
            self.assertTrue('event_identity' in ev)
            self.assertEqual(dict, type(ev['event_identity']))
            self.assertTrue(ev['event_identity']['path'].rsplit('/', 1)[1])

            self.assertTrue('logical_resource_id' in ev)
            self.assertEqual('WebServer', ev['logical_resource_id'])

            self.assertTrue('physical_resource_id' in ev)

            self.assertTrue('resource_properties' in ev)
            # Big long user data field.. it mentions 'wordpress'
            # a few times so this should work.
            user_data = ev['resource_properties']['UserData']
            self.assertNotEqual(user_data.find('wordpress'), -1)
            self.assertEqual('F17-x86_64-gold',
                             ev['resource_properties']['ImageId'])
            self.assertEqual('m1.large',
                             ev['resource_properties']['InstanceType'])

            self.assertEqual('CREATE', ev['resource_action'])
            self.assertTrue(ev['resource_status'] in ('IN_PROGRESS',
                                                      'COMPLETE'))

            self.assertTrue('resource_status_reason' in ev)
            self.assertEqual('state changed', ev['resource_status_reason'])

            self.assertTrue('resource_type' in ev)
            self.assertEqual('AWS::EC2::Instance', ev['resource_type'])

            self.assertTrue('stack_identity' in ev)

            self.assertTrue('stack_name' in ev)
            self.assertEqual(self.stack.name, ev['stack_name'])

            self.assertTrue('event_time' in ev)

        self.m.VerifyAll()

    @stack_context('service_event_list_test_stack')
    def test_stack_event_list_by_tenant(self):
        events = self.eng.list_events(self.ctx, None)

        self.assertEqual(2, len(events))
        for ev in events:
            self.assertIn('event_identity', ev)
            self.assertThat(ev['event_identity'], matchers.IsInstance(dict))
            self.assertTrue(ev['event_identity']['path'].rsplit('/', 1)[1])

            self.assertTrue('logical_resource_id' in ev)
            self.assertEqual('WebServer', ev['logical_resource_id'])

            self.assertTrue('physical_resource_id' in ev)

            self.assertTrue('resource_properties' in ev)
            # Big long user data field.. it mentions 'wordpress'
            # a few times so this should work.
            user_data = ev['resource_properties']['UserData']
            self.assertIn('wordpress', user_data)
            self.assertEqual('F17-x86_64-gold',
                             ev['resource_properties']['ImageId'])
            self.assertEqual('m1.large',
                             ev['resource_properties']['InstanceType'])

            self.assertEqual('CREATE', ev['resource_action'])
            self.assertIn(ev['resource_status'], ('IN_PROGRESS', 'COMPLETE'))

            self.assertIn('resource_status_reason', ev)
            self.assertEqual('state changed', ev['resource_status_reason'])

            self.assertIn('resource_type', ev)
            self.assertEqual('AWS::EC2::Instance', ev['resource_type'])

            self.assertIn('stack_identity', ev)

            self.assertIn('stack_name', ev)
            self.assertEqual(self.stack.name, ev['stack_name'])

            self.assertIn('event_time', ev)

        self.m.VerifyAll()

    @stack_context('service_list_all_test_stack')
    def test_stack_list_all(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx, stack=mox.IgnoreArg(), resolve_data=False)\
            .AndReturn(self.stack)

        self.m.ReplayAll()
        sl = self.eng.list_stacks(self.ctx)

        self.assertEqual(1, len(sl))
        for s in sl:
            self.assertTrue('creation_time' in s)
            self.assertTrue('updated_time' in s)
            self.assertTrue('stack_identity' in s)
            self.assertNotEqual(s['stack_identity'], None)
            self.assertTrue('stack_name' in s)
            self.assertEqual(self.stack.name, s['stack_name'])
            self.assertTrue('stack_status' in s)
            self.assertTrue('stack_status_reason' in s)
            self.assertTrue('description' in s)
            self.assertNotEqual(s['description'].find('WordPress'), -1)

        self.m.VerifyAll()

    def test_stack_describe_nonexistent(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(exception.StackNotFound)
        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.eng.show_stack,
                          self.ctx, non_exist_identifier)
        self.m.VerifyAll()

    def test_stack_describe_bad_tenant(self):
        non_exist_identifier = identifier.HeatIdentifier(
            'wibble', 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(exception.InvalidTenant)
        self.m.ReplayAll()

        self.assertRaises(exception.InvalidTenant,
                          self.eng.show_stack,
                          self.ctx, non_exist_identifier)

        self.m.VerifyAll()

    @stack_context('service_describe_test_stack', False)
    def test_stack_describe(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = db_api.stack_get(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.ReplayAll()

        sl = self.eng.show_stack(self.ctx, self.stack.identifier())

        self.assertEqual(1, len(sl))

        s = sl[0]
        self.assertTrue('creation_time' in s)
        self.assertTrue('updated_time' in s)
        self.assertTrue('stack_identity' in s)
        self.assertNotEqual(s['stack_identity'], None)
        self.assertTrue('stack_name' in s)
        self.assertEqual(self.stack.name, s['stack_name'])
        self.assertTrue('stack_status' in s)
        self.assertTrue('stack_status_reason' in s)
        self.assertTrue('description' in s)
        self.assertNotEqual(s['description'].find('WordPress'), -1)
        self.assertTrue('parameters' in s)

        self.m.VerifyAll()

    @stack_context('service_describe_all_test_stack', False)
    def test_stack_describe_all(self):
        sl = self.eng.show_stack(self.ctx, None)

        self.assertEqual(1, len(sl))

        s = sl[0]
        self.assertTrue('creation_time' in s)
        self.assertTrue('updated_time' in s)
        self.assertTrue('stack_identity' in s)
        self.assertNotEqual(s['stack_identity'], None)
        self.assertTrue('stack_name' in s)
        self.assertEqual(self.stack.name, s['stack_name'])
        self.assertTrue('stack_status' in s)
        self.assertTrue('stack_status_reason' in s)
        self.assertTrue('description' in s)
        self.assertNotEqual(s['description'].find('WordPress'), -1)
        self.assertTrue('parameters' in s)

    @stack_context('service_list_resource_types_test_stack', False)
    def test_list_resource_types(self):
        resources = self.eng.list_resource_types(self.ctx)
        self.assertTrue(isinstance(resources, list))
        self.assertTrue('AWS::EC2::Instance' in resources)

    @stack_context('service_stack_resource_describe__test_stack')
    def test_stack_resource_describe(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        r = self.eng.describe_stack_resource(self.ctx, self.stack.identifier(),
                                             'WebServer')

        self.assertTrue('resource_identity' in r)
        self.assertTrue('description' in r)
        self.assertTrue('updated_time' in r)
        self.assertTrue('stack_identity' in r)
        self.assertNotEqual(r['stack_identity'], None)
        self.assertTrue('stack_name' in r)
        self.assertEqual(self.stack.name, r['stack_name'])
        self.assertTrue('metadata' in r)
        self.assertTrue('resource_status' in r)
        self.assertTrue('resource_status_reason' in r)
        self.assertTrue('resource_type' in r)
        self.assertTrue('physical_resource_id' in r)
        self.assertTrue('logical_resource_id' in r)
        self.assertEqual('WebServer', r['logical_resource_id'])

        self.m.VerifyAll()

    def test_stack_resource_describe_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id,
            'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(exception.StackNotFound)
        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.eng.describe_stack_resource,
                          self.ctx, non_exist_identifier, 'WebServer')

        self.m.VerifyAll()

    @stack_context('service_resource_describe_nonexist_test_stack')
    def test_stack_resource_describe_nonexist_resource(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)

        self.m.ReplayAll()
        self.assertRaises(exception.ResourceNotFound,
                          self.eng.describe_stack_resource,
                          self.ctx, self.stack.identifier(), 'foo')

        self.m.VerifyAll()

    @stack_context('service_resource_describe_user_deny_test_stack')
    def test_stack_resource_describe_stack_user_deny(self):
        self.ctx.roles = [cfg.CONF.heat_stack_user_role]
        self.m.StubOutWithMock(service.EngineService, '_authorize_stack_user')
        service.EngineService._authorize_stack_user(self.ctx, mox.IgnoreArg(),
                                                    'foo').AndReturn(False)
        self.m.ReplayAll()

        self.assertRaises(exception.Forbidden,
                          self.eng.describe_stack_resource,
                          self.ctx, self.stack.identifier(), 'foo')

        self.m.VerifyAll()

    @stack_context('service_authorize_stack_user_nocreds_test_stack')
    def test_stack_authorize_stack_user_nocreds(self):
        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))

    @stack_context('service_authorize_user_attribute_error_test_stack')
    def test_stack_authorize_stack_user_attribute_error(self):
        self.m.StubOutWithMock(json, 'loads')
        json.loads(None).AndRaise(AttributeError)
        self.m.ReplayAll()
        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))
        self.m.VerifyAll()

    @stack_context('service_authorize_stack_user_type_error_test_stack')
    def test_stack_authorize_stack_user_type_error(self):
        self.m.StubOutWithMock(json, 'loads')
        json.loads(mox.IgnoreArg()).AndRaise(TypeError)
        self.m.ReplayAll()

        self.assertFalse(self.eng._authorize_stack_user(self.ctx,
                                                        self.stack,
                                                        'foo'))

        self.m.VerifyAll()

    @stack_context('service_resources_describe_test_stack')
    def test_stack_resources_describe(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      'WebServer')

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertTrue('resource_identity' in r)
        self.assertTrue('description' in r)
        self.assertTrue('updated_time' in r)
        self.assertTrue('stack_identity' in r)
        self.assertNotEqual(r['stack_identity'], None)
        self.assertTrue('stack_name' in r)
        self.assertEqual(self.stack.name, r['stack_name'])
        self.assertTrue('resource_status' in r)
        self.assertTrue('resource_status_reason' in r)
        self.assertTrue('resource_type' in r)
        self.assertTrue('physical_resource_id' in r)
        self.assertTrue('logical_resource_id' in r)
        self.assertEqual('WebServer', r['logical_resource_id'])

        self.m.VerifyAll()

    @stack_context('service_resources_describe_no_filter_test_stack')
    def test_stack_resources_describe_no_filter(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      None)

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertTrue('logical_resource_id' in r)
        self.assertEqual('WebServer', r['logical_resource_id'])

        self.m.VerifyAll()

    def test_stack_resources_describe_bad_lookup(self):
        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, None).AndRaise(TypeError)
        self.m.ReplayAll()

        self.assertRaises(TypeError,
                          self.eng.describe_stack_resources,
                          self.ctx, None, 'WebServer')
        self.m.VerifyAll()

    def test_stack_resources_describe_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.assertRaises(exception.StackNotFound,
                          self.eng.describe_stack_resources,
                          self.ctx, non_exist_identifier, 'WebServer')

    @stack_context('service_find_physical_resource_test_stack')
    def test_find_physical_resource(self):
        resources = self.eng.describe_stack_resources(self.ctx,
                                                      self.stack.identifier(),
                                                      None)
        phys_id = resources[0]['physical_resource_id']

        result = self.eng.find_physical_resource(self.ctx, phys_id)
        self.assertTrue(isinstance(result, dict))
        resource_identity = identifier.ResourceIdentifier(**result)
        self.assertEqual(self.stack.identifier(), resource_identity.stack())
        self.assertEqual('WebServer', resource_identity.resource_name)

    def test_find_physical_resource_nonexist(self):
        self.assertRaises(exception.PhysicalResourceNotFound,
                          self.eng.find_physical_resource,
                          self.ctx, 'foo')

    @stack_context('service_resources_list_test_stack')
    def test_stack_resources_list(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        resources = self.eng.list_stack_resources(self.ctx,
                                                  self.stack.identifier())

        self.assertEqual(1, len(resources))
        r = resources[0]
        self.assertTrue('resource_identity' in r)
        self.assertTrue('updated_time' in r)
        self.assertTrue('physical_resource_id' in r)
        self.assertTrue('logical_resource_id' in r)
        self.assertEqual('WebServer', r['logical_resource_id'])
        self.assertTrue('resource_status' in r)
        self.assertTrue('resource_status_reason' in r)
        self.assertTrue('resource_type' in r)

        self.m.VerifyAll()

    def test_stack_resources_list_nonexist_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(exception.StackNotFound)
        self.m.ReplayAll()

        self.assertRaises(exception.StackNotFound,
                          self.eng.list_stack_resources,
                          self.ctx, non_exist_identifier)

        self.m.VerifyAll()

    @stack_context('service_metadata_test_stack')
    def test_metadata(self):
        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        pre_update_meta = self.stack['WebServer'].metadata

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        s = db_api.stack_get(self.ctx, self.stack.id)
        service.EngineService._get_stack(self.ctx,
                                         self.stack.identifier()).AndReturn(s)
        self.m.StubOutWithMock(instances.Instance, 'metadata_update')
        self.m.StubOutWithMock(db_api, 'user_creds_get')
        instances.Instance.metadata_update(new_metadata=test_metadata)
        db_api.user_creds_get(mox.IgnoreArg()).MultipleTimes().AndReturn(
            self.ctx.to_dict())
        self.m.ReplayAll()

        result = self.eng.metadata_update(self.ctx,
                                          dict(self.stack.identifier()),
                                          'WebServer', test_metadata)
        # metadata_update is a no-op for all resources except
        # WaitConditionHandle so we don't expect this to have changed
        self.assertEqual(pre_update_meta, result)

        self.m.VerifyAll()

    def test_metadata_err_stack(self):
        non_exist_identifier = identifier.HeatIdentifier(
            self.ctx.tenant_id, 'wibble',
            '18d06e2e-44d3-4bef-9fbf-52480d604b02')

        self.m.StubOutWithMock(service.EngineService, '_get_stack')
        service.EngineService._get_stack(
            self.ctx, non_exist_identifier).AndRaise(exception.StackNotFound)
        self.m.ReplayAll()

        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        self.assertRaises(exception.StackNotFound,
                          self.eng.metadata_update,
                          self.ctx, non_exist_identifier,
                          'WebServer', test_metadata)
        self.m.VerifyAll()

    @stack_context('service_metadata_err_resource_test_stack', False)
    def test_metadata_err_resource(self):
        self.m.StubOutWithMock(parser.Stack, 'load')
        parser.Stack.load(self.ctx,
                          stack=mox.IgnoreArg()).AndReturn(self.stack)
        self.m.ReplayAll()

        test_metadata = {'foo': 'bar', 'baz': 'quux', 'blarg': 'wibble'}
        self.assertRaises(exception.ResourceNotFound,
                          self.eng.metadata_update,
                          self.ctx, dict(self.stack.identifier()),
                          'NooServer', test_metadata)

        self.m.VerifyAll()

    @stack_context('periodic_watch_task_not_created')
    def test_periodic_watch_task_not_created(self):
        self.eng.stg[self.stack.id] = DummyThreadGroup()
        self.eng._start_watch_task(self.stack.id, self.ctx)
        self.assertEqual([], self.eng.stg[self.stack.id].threads)

    def test_periodic_watch_task_created(self):
        stack = get_alarm_stack('period_watch_task_created',
                                create_context(self.m))
        self.stack = stack
        self.m.ReplayAll()
        stack.store()
        stack.create()
        self.eng.stg[stack.id] = DummyThreadGroup()
        self.eng._start_watch_task(stack.id, self.ctx)
        self.assertEqual([self.eng._periodic_watcher_task],
                         self.eng.stg[stack.id].threads)
        self.stack.delete()

    @stack_context('service_show_watch_test_stack', False)
    @utils.wr_delete_after
    def test_show_watch(self):
        # Insert two dummy watch rules into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = []
        self.wr.append(watchrule.WatchRule(context=self.ctx,
                                           watch_name='HttpFailureAlarm',
                                           rule=rule,
                                           watch_data=[],
                                           stack_id=self.stack.id,
                                           state='NORMAL'))
        self.wr[0].store()

        self.wr.append(watchrule.WatchRule(context=self.ctx,
                                           watch_name='AnotherWatch',
                                           rule=rule,
                                           watch_data=[],
                                           stack_id=self.stack.id,
                                           state='NORMAL'))
        self.wr[1].store()

        # watch_name=None should return both watches
        result = self.eng.show_watch(self.ctx, watch_name=None)
        self.assertEqual(2, len(result))

        # watch_name="HttpFailureAlarm" should return only one
        result = self.eng.show_watch(self.ctx, watch_name="HttpFailureAlarm")
        self.assertEqual(1, len(result))

        self.assertRaises(exception.WatchRuleNotFound,
                          self.eng.show_watch,
                          self.ctx, watch_name="nonexistent")

        # Check the response has all keys defined in the engine API
        for key in engine_api.WATCH_KEYS:
            self.assertTrue(key in result[0])

    @stack_context('service_show_watch_metric_test_stack', False)
    @utils.wr_delete_after
    def test_show_watch_metric(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='HttpFailureAlarm',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        # And add a metric datapoint
        watch = db_api.watch_rule_get_by_name(self.ctx, "HttpFailureAlarm")
        self.assertNotEqual(watch, None)
        values = {'watch_rule_id': watch.id,
                  'data': {u'Namespace': u'system/linux',
                           u'ServiceFailure': {
                           u'Units': u'Counter', u'Value': 1}}}
        watch = db_api.watch_data_create(self.ctx, values)

        # Check there is one result returned
        result = self.eng.show_watch_metric(self.ctx,
                                            metric_namespace=None,
                                            metric_name=None)
        self.assertEqual(1, len(result))

        # Create another metric datapoint and check we get two
        watch = db_api.watch_data_create(self.ctx, values)
        result = self.eng.show_watch_metric(self.ctx,
                                            metric_namespace=None,
                                            metric_name=None)
        self.assertEqual(2, len(result))

        # Check the response has all keys defined in the engine API
        for key in engine_api.WATCH_DATA_KEYS:
            self.assertTrue(key in result[0])

    @stack_context('service_show_watch_state_test_stack')
    @utils.wr_delete_after
    def test_set_watch_state(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='OverrideAlarm',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        class DummyAction(object):
            alarm = "dummyfoo"

        dummy_action = DummyAction()
        self.m.StubOutWithMock(parser.Stack, '__getitem__')
        parser.Stack.__getitem__(
            'WebServerRestartPolicy').AndReturn(dummy_action)

        # Replace the real stack threadgroup with a dummy one, so we can
        # check the function returned on ALARM is correctly scheduled
        self.eng.stg[self.stack.id] = DummyThreadGroup()

        self.m.ReplayAll()

        state = watchrule.WatchRule.NODATA
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[engine_api.WATCH_STATE_VALUE])
        self.assertEqual([], self.eng.stg[self.stack.id].threads)

        state = watchrule.WatchRule.NORMAL
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[engine_api.WATCH_STATE_VALUE])
        self.assertEqual([], self.eng.stg[self.stack.id].threads)

        state = watchrule.WatchRule.ALARM
        result = self.eng.set_watch_state(self.ctx,
                                          watch_name="OverrideAlarm",
                                          state=state)
        self.assertEqual(state, result[engine_api.WATCH_STATE_VALUE])
        self.assertEqual([DummyAction.alarm],
                         self.eng.stg[self.stack.id].threads)

        self.m.VerifyAll()

    @stack_context('service_show_watch_state_badstate_test_stack')
    @utils.wr_delete_after
    def test_set_watch_state_badstate(self):
        # Insert dummy watch rule into the DB
        rule = {u'EvaluationPeriods': u'1',
                u'AlarmActions': [u'WebServerRestartPolicy'],
                u'AlarmDescription': u'Restart the WikiDatabase',
                u'Namespace': u'system/linux',
                u'Period': u'300',
                u'ComparisonOperator': u'GreaterThanThreshold',
                u'Statistic': u'SampleCount',
                u'Threshold': u'2',
                u'MetricName': u'ServiceFailure'}
        self.wr = watchrule.WatchRule(context=self.ctx,
                                      watch_name='OverrideAlarm2',
                                      rule=rule,
                                      watch_data=[],
                                      stack_id=self.stack.id,
                                      state='NORMAL')
        self.wr.store()

        self.m.StubOutWithMock(watchrule.WatchRule, 'set_watch_state')
        for state in ["HGJHGJHG", "1234", "!\*(&%"]:
            watchrule.WatchRule.set_watch_state(state)\
                .InAnyOrder().AndRaise(ValueError)
        self.m.ReplayAll()

        for state in ["HGJHGJHG", "1234", "!\*(&%"]:
            self.assertRaises(ValueError,
                              self.eng.set_watch_state,
                              self.ctx, watch_name="OverrideAlarm2",
                              state=state)

        self.m.VerifyAll()

    def test_set_watch_state_noexist(self):
        state = watchrule.WatchRule.ALARM   # State valid

        self.m.StubOutWithMock(watchrule.WatchRule, 'load')
        watchrule.WatchRule.load(self.ctx, "nonexistent")\
            .AndRaise(exception.WatchRuleNotFound)
        self.m.ReplayAll()

        self.assertRaises(exception.WatchRuleNotFound,
                          self.eng.set_watch_state,
                          self.ctx, watch_name="nonexistent", state=state)
        self.m.VerifyAll()

    def test_stack_list_all_empty(self):
        sl = self.eng.list_stacks(self.ctx)

        self.assertEqual(0, len(sl))

    def test_stack_describe_all_empty(self):
        sl = self.eng.show_stack(self.ctx, None)

        self.assertEqual(0, len(sl))
