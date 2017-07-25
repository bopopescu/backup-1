# Copyright 2016 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""service-management deploy command."""

import os

from googlecloudsdk.api_lib.service_management import config_reporter
from googlecloudsdk.api_lib.service_management import enable_api
from googlecloudsdk.api_lib.service_management import exceptions
from googlecloudsdk.api_lib.service_management import services_util
from googlecloudsdk.calliope import base
from googlecloudsdk.calliope import exceptions as calliope_exceptions
from googlecloudsdk.core import log
from googlecloudsdk.core import properties


_DETAILED_HELP = {
    'DESCRIPTION': """\
        This command is used to deploy a service configuration for a service
        to Google Service Management. As input, it takes one or more paths
        to service configurations that should be uploaded. These configuration
        files can be Proto Descriptors, Open API (Swagger) specifications,
        or Google Service Configuration files in JSON or YAML formats.

        If a service name is present in multiple configuration files (given
        in the `host` field in OpenAPI specifications or the `name` field in
        Google Service Configuration files), the first one will take precedence.

        This command will block until deployment is complete unless the
        `--async` flag is passed.
        """,
    'EXAMPLES': """\
        To deploy a single Open API service configuration, run:

          $ {command} ~/my_app/openapi.json

        To run the deployment asynchronously (non-blocking), run:

          $ {command} ~/my_app/openapi.json --async

        To deploy a service config with a Proto, run:

          $ {command} ~/my_app/service-config.yaml ~/my_app/service-protos.pb
        """,
}

_BETA_DETAILED_HELP = {
    'DESCRIPTION': """\
        This command is used to deploy a service configuration for a service
        to Google Service Management. As input, it takes one or more paths
        to service configurations that should be uploaded. These configuration
        files can be Proto Descriptors, Open API (Swagger) specifications,
        or Google Service Configuration files in JSON or YAML formats.

        If a service name is present in multiple configuration files (given
        in the `host` field in OpenAPI specifications or the `name` field in
        Google Service Configuration files), the first one will take precedence.

        When deploying a new service configuration to an already-existing
        service, some safety checks will be made comparing the new configuration
        to the active configuration. If any actionable advice is provided, it
        will be printed out to the log, and the deployment will be halted. It is
        recommended that these warnings be addressed before proceeding, but they
        can be overridden with the --force flag.

        This command will block until deployment is complete unless the
        `--async` flag is passed.
        """,
    'EXAMPLES': """\
        To deploy a single Open API service configuration, run:

          $ {command} ~/my_app/openapi.json

        To run the deployment asynchronously (non-blocking), run:

          $ {command} ~/my_app/openapi.json --async

        To deploy a service config with a Proto, run:

          $ {command} ~/my_app/service-config.yaml ~/my_app/service-protos.pb
        """,
}

ADVICE_STRING = ('Advice found for changes in the new service config. If this '
                 'is a --validate-only run, the config push would have failed. '
                 'See the outputted report for failure reason(s). If this is '
                 'not a --validate-only run and you would like to ignore these '
                 'warnings, rerun the command with --force. NOTE: setting this '
                 'flag will ignore all change advice. For production systems, '
                 'best practice is to set this for a single execution only '
                 'after manually reviewing all changes with advice.')

FORCE_ADVICE_STRING = ('Advice found for changes in the new service config, '
                       'but proceeding anyway because --force is set...')

NUM_ADVICE_TO_PRINT = 3


def _CommonArgs(parser):
  parser.add_argument(
      'service_config_file',
      nargs='+',
      help=('The service configuration file (or files) containing the API '
            'specification to upload. Proto Descriptors, Open API (Swagger) '
            'specifications, and Google Service Configuration files in JSON '
            'and YAML formats are acceptable.'))
  base.ASYNC_FLAG.AddToParser(parser)


class _BaseDeploy(object):
  """Create deploy base class for all release tracks."""

  @staticmethod
  def Args(parser):
    """Args is called by calliope to gather arguments for this command.

    Args:
      parser: An argparse parser that you can use to add arguments that go
          on the command line after this command. Positional arguments are
          allowed.
    """
    _CommonArgs(parser)
    parser.add_argument(
        '--validate-only',
        action='store_true',
        help='If included, the command will only validate the service '
             'configuration(s). No configuration(s) will be persisted.')

    parser.add_argument(
        '--force',
        '-f',
        action='store_true',
        default=False,
        help='Force the deployment even if any hazardous changes to the '
             'service configuration are detected.')

  def MakeConfigFileMessage(self, file_contents, filename, file_type):
    """Constructs a ConfigFile message from a config file.

    Args:
      file_contents: The contents of the config file.
      filename: The full path to the config file.
      file_type: FileTypeValueValuesEnum describing the type of config file.

    Returns:
      The constructed ConfigFile message.
    """

    messages = services_util.GetMessagesModule()
    return messages.ConfigFile(
        fileContents=file_contents,
        filePath=os.path.basename(filename),
        fileType=file_type,)

  def ShowConfigReport(self, service, service_config_id, log_func=log.warn):
    """Run and display results (if any) from the Push Advisor.

    Args:
      service: The name of the service for which to compare configs.
      service_config_id: The new config ID to compare against the active config.
      log_func: The function to which to pass advisory messages
        (default: log.warn).

    Returns:
      The number of advisory messages returned by the Push Advisor.
    """
    num_changes_with_advice = 0

    reporter = config_reporter.ConfigReporter(service)

    # Set the new config as the recently generated service config ID
    reporter.new_config.SetConfigId(service_config_id)

    # We always want to compare agaisnt the active config, so use default here
    reporter.old_config.SetConfigUseDefaultId()

    change_report = reporter.RunReport()
    if not change_report or not change_report.configChanges:
      return 0

    changes = change_report.configChanges

    for change in changes:
      if change.advices:
        if num_changes_with_advice < NUM_ADVICE_TO_PRINT:
          log_func(services_util.PushAdvisorConfigChangeToString(change))
        num_changes_with_advice += 1

    if num_changes_with_advice > NUM_ADVICE_TO_PRINT:
      log_func('%s total changes with advice found, check config report file '
               'for full list.', num_changes_with_advice)

    return num_changes_with_advice

  def CheckPushAdvisor(self, unused_force=False):
    """Run the Push Advisor and return whether the command should abort.

    Args:
      unused_force: bool, unused in the default implementation.

    Returns:
      True if the deployment should be aborted due to warnings, otherwise
      False if it's safe to continue.
    """
    # Child classes must override this; otherwise, we'll always return False
    return False

  def Run(self, args):
    """Run 'service-management deploy'.

    Args:
      args: argparse.Namespace, The arguments that this command was invoked
          with.

    Returns:
      The response from the Update API call.

    Raises:
      BadFileExceptionn: if the provided service configuration files are
          invalid or cannot be read.
    """
    messages = services_util.GetMessagesModule()
    client = services_util.GetClientInstance()

    file_types = messages.ConfigFile.FileTypeValueValuesEnum
    self.service_name = self.service_version = config_contents = None
    config_files = []

    self.validate_only = args.validate_only

    # If we're not doing a validate-only run, we don't want to output the
    # resource directly unless the user specifically requests it using the
    # --format flag. The Epilog will show useful information after deployment
    # is complete.
    if not self.validate_only and not args.IsSpecified('format'):
      args.format = 'none'

    for service_config_file in args.service_config_file:
      config_contents = services_util.ReadServiceConfigFile(service_config_file)

      if services_util.FilenameMatchesExtension(
          service_config_file, ['.json', '.yaml', '.yml']):
        # Try to load the file as JSON. If that fails, try YAML.
        service_config_dict = services_util.LoadJsonOrYaml(config_contents)
        if not service_config_dict:
          raise calliope_exceptions.BadFileException(
              'Could not read JSON or YAML from service config file '
              '[{0}].'.format(service_config_file))

        if 'swagger' in service_config_dict:
          if 'host' not in service_config_dict:
            raise calliope_exceptions.BadFileException((
                'Malformed input. Found Swagger service config in file [{}], '
                'but no host was specified. Add a host specification to the '
                'config file.').format(
                    service_config_file))
          if not self.service_name and service_config_dict.get('host'):
            self.service_name = service_config_dict.get('host')

          # Always use YAML for Open API because JSON is a subset of YAML.
          config_files.append(
              self.MakeConfigFileMessage(config_contents, service_config_file,
                                         file_types.OPEN_API_YAML))
        elif service_config_dict.get('type') == 'google.api.Service':
          if not self.service_name and service_config_dict.get('name'):
            self.service_name = service_config_dict.get('name')

          config_files.append(
              self.MakeConfigFileMessage(config_contents, service_config_file,
                                         file_types.SERVICE_CONFIG_YAML))
        elif 'name' in service_config_dict:
          # This is a special case. If we have been provided a Google Service
          # Configuration file which has a service 'name' field, but no 'type'
          # field, we have to assume that this is a normalized service config,
          # and can be uploaded via the CreateServiceConfig API. Therefore,
          # we can short circute the process here.
          if len(args.service_config_file) > 1:
            raise calliope_exceptions.BadFileException((
                'Ambiguous input. Found normalized service configuration in '
                'file [{0}], but received multiple input files. To upload '
                'normalized service config, please provide it separately from '
                'other input files to avoid ambiguity.').format(
                    service_config_file))

          # If this is a validate-only run, abort now, since this is not
          # supported in the ServiceConfigs.Create API
          if self.validate_only:
            raise exceptions.InvalidFlagError(
                'The --validate-only flag is not supported when using '
                'normalized service configs as input.')

          self.service_name = service_config_dict.get('name')
          config_files = []
          break
        else:
          raise calliope_exceptions.BadFileException((
              'Unable to parse Open API, or Google Service Configuration '
              'specification from {0}').format(service_config_file))

      elif services_util.IsProtoDescriptor(service_config_file):
        config_files.append(
            self.MakeConfigFileMessage(config_contents, service_config_file,
                                       file_types.FILE_DESCRIPTOR_SET_PROTO))
      else:
        raise calliope_exceptions.BadFileException((
            'Could not determine the content type of file [{0}]. Supported '
            'extensions are .json .yaml .yml .pb. and .descriptor').format(
                service_config_file))

    # Check to see if the Endpoints meta service needs to be enabled.
    enable_api.EnableServiceIfDisabled(
        properties.VALUES.core.project.Get(required=True),
        services_util.GetEndpointsServiceName(),
        args.async)
    # Check if we need to create the service.
    services_util.CreateServiceIfNew(
        self.service_name, properties.VALUES.core.project.Get(required=True))

    if config_files:
      push_config_result = services_util.PushMultipleServiceConfigFiles(
          self.service_name, config_files, args.async,
          validate_only=self.validate_only)
      self.service_config_id = (
          services_util.GetServiceConfigIdFromSubmitConfigSourceResponse(
              push_config_result)
      )
    else:
      push_config_result = services_util.PushNormalizedGoogleServiceConfig(
          self.service_name,
          properties.VALUES.core.project.Get(required=True),
          config_contents)
      self.service_config_id = push_config_result.id

    if not self.service_config_id:
      raise exceptions.InvalidConditionError(
          'Failed to retrieve Service Configuration Id.')

    # Run the Push Advisor to see if we need to warn the user of any
    # potentially hazardous changes to the service configuration.
    if self.CheckPushAdvisor(args.force):
      return None

    # Create a Rollout for the new service configuration
    if not self.validate_only:
      percentages = messages.TrafficPercentStrategy.PercentagesValue()
      percentages.additionalProperties.append(
          (messages.TrafficPercentStrategy.PercentagesValue.AdditionalProperty(
              key=self.service_config_id, value=100.0)))
      traffic_percent_strategy = messages.TrafficPercentStrategy(
          percentages=percentages)
      rollout = messages.Rollout(
          serviceName=self.service_name,
          trafficPercentStrategy=traffic_percent_strategy,)
      rollout_operation = client.services_rollouts.Create(rollout)
      services_util.ProcessOperationResult(rollout_operation, args.async)

      # Check to see if the service is already enabled
      enable_api.EnableServiceIfDisabled(
          properties.VALUES.core.project.Get(required=True),
          self.service_name,
          args.async)

    return push_config_result

  def Epilog(self, resources_were_displayed):
    # Print this to screen not to the log because the output is needed by the
    # human user. Only print this when not doing a validate-only run.
    if resources_were_displayed and not self.validate_only:
      log.status.Print(
          ('\nService Configuration [{0}] uploaded for '
           'service [{1}]\n').format(self.service_config_id, self.service_name))

      management_url = services_util.GenerateManagementUrl(
          self.service_name, properties.VALUES.core.project.Get(required=True))
      log.status.Print('To manage your API, go to: ' + management_url)


@base.ReleaseTracks(base.ReleaseTrack.GA)
class Deploy(_BaseDeploy, base.Command):
  """Deploys a service configuration for the given service name."""


@base.ReleaseTracks(base.ReleaseTrack.BETA)
class DeployBeta(_BaseDeploy, base.Command):
  """Deploys a service configuration for the given service name."""

  def CheckPushAdvisor(self, force=False):
    """Run the Push Advisor and return whether the command should abort.

    Args:
      force: bool, if True, this method will return False even if warnings are
        generated.

    Returns:
      True if the deployment should be aborted due to warnings, otherwise
      False if it's safe to continue.
    """
    log_func = log.warn if force else log.error
    num_advices = self.ShowConfigReport(
        self.service_name, self.service_config_id, log_func)
    if num_advices > 0:
      if force:
        log_func(FORCE_ADVICE_STRING)
      else:
        log_func(ADVICE_STRING)
        return True

    return False


@base.ReleaseTracks(base.ReleaseTrack.ALPHA)
class DeployAlpha(_BaseDeploy, base.Command):
  """Deploys a service configuration for the given service name."""

  def CheckPushAdvisor(self, force=False):
    """Run the Push Advisor and return whether the command should abort.

    Args:
      force: bool, if True, this method will return False even if warnings are
        generated.

    Returns:
      True if the deployment should be aborted due to warnings, otherwise
      False if it's safe to continue.
    """
    log_func = log.warn if force else log.error
    num_advices = self.ShowConfigReport(
        self.service_name, self.service_config_id, log_func)
    if num_advices > 0:
      if force:
        log_func(FORCE_ADVICE_STRING)
      else:
        log_func(ADVICE_STRING)
        return True

    return False


Deploy.detailed_help = _DETAILED_HELP
DeployBeta.detailed_help = _BETA_DETAILED_HELP
DeployAlpha.detailed_help = _BETA_DETAILED_HELP