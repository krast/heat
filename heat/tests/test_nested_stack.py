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


from heat.common import exception
from heat.common import template_format
from heat.common import urlfetch
from heat.db import api as db_api
from heat.engine import parser
from heat.engine import resource
from heat.engine import scheduler
from heat.tests import generic_resource as generic_rsrc
from heat.tests import utils
from heat.tests.common import HeatTestCase
from heat.tests.utils import dummy_context
from heat.tests.utils import setup_dummy_db


class NestedStackTest(HeatTestCase):
    test_template = '''
HeatTemplateFormatVersion: '2012-12-12'
Resources:
  the_nested:
    Type: AWS::CloudFormation::Stack
    Properties:
      TemplateURL: https://localhost/the.template
      Parameters:
        KeyName: foo
'''

    nested_template = '''
HeatTemplateFormatVersion: '2012-12-12'
Parameters:
  KeyName:
    Type: String
Outputs:
  Foo:
    Value: bar
'''

    def setUp(self):
        super(NestedStackTest, self).setUp()
        self.m.StubOutWithMock(urlfetch, 'get')
        setup_dummy_db()

    def create_stack(self, template):
        t = template_format.parse(template)
        stack = self.parse_stack(t)
        stack.create()
        self.assertEqual(stack.state, (stack.CREATE, stack.COMPLETE))
        return stack

    def parse_stack(self, t):
        ctx = dummy_context('test_username', 'aaaa', 'password')
        stack_name = 'test_stack'
        tmpl = parser.Template(t)
        stack = parser.Stack(ctx, stack_name, tmpl)
        stack.store()
        return stack

    def test_nested_stack(self):
        urlfetch.get('https://localhost/the.template').AndReturn(
            self.nested_template)
        self.m.ReplayAll()

        stack = self.create_stack(self.test_template)
        rsrc = stack['the_nested']
        nested_name = utils.PhysName(stack.name, 'the_nested')
        self.assertEqual(nested_name, rsrc.physical_resource_name())
        arn_prefix = ('arn:openstack:heat::aaaa:stacks/%s/' %
                      rsrc.physical_resource_name())
        self.assertTrue(rsrc.FnGetRefId().startswith(arn_prefix))

        self.assertRaises(resource.UpdateReplace,
                          rsrc.handle_update, {}, {}, {})

        self.assertEqual('bar', rsrc.FnGetAtt('Outputs.Foo'))
        self.assertRaises(
            exception.InvalidTemplateAttribute, rsrc.FnGetAtt, 'Foo')

        rsrc.delete()
        self.assertTrue(rsrc.FnGetRefId().startswith(arn_prefix))

        self.m.VerifyAll()

    def test_nested_stack_suspend_resume(self):
        urlfetch.get('https://localhost/the.template').AndReturn(
            self.nested_template)
        self.m.ReplayAll()

        stack = self.create_stack(self.test_template)
        rsrc = stack['the_nested']

        scheduler.TaskRunner(rsrc.suspend)()
        self.assertEqual(rsrc.state, (rsrc.SUSPEND, rsrc.COMPLETE))

        scheduler.TaskRunner(rsrc.resume)()
        self.assertEqual(rsrc.state, (rsrc.RESUME, rsrc.COMPLETE))

        rsrc.delete()
        self.m.VerifyAll()


class ResDataResource(generic_rsrc.GenericResource):
    def handle_create(self):
        db_api.resource_data_set(self, "test", 'A secret value', True)


class ResDataNestedStackTest(NestedStackTest):

    nested_template = '''
HeatTemplateFormatVersion: "2012-12-12"
Parameters:
  KeyName:
    Type: String
Resources:
  nested_res:
    Type: "res.data.resource"
Outputs:
  Foo:
    Value: bar
'''

    def setUp(self):
        resource._register_class("res.data.resource", ResDataResource)
        super(ResDataNestedStackTest, self).setUp()

    def test_res_data_delete(self):
        urlfetch.get('https://localhost/the.template').AndReturn(
            self.nested_template)
        self.m.ReplayAll()
        stack = self.create_stack(self.test_template)
        res = stack['the_nested'].nested()['nested_res']
        stack.delete()
        self.assertEqual(stack.state, (stack.DELETE, stack.COMPLETE))
        self.assertRaises(exception.NotFound, db_api.resource_data_get, res,
                          'test')
