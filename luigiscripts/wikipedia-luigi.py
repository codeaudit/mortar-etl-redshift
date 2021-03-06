import luigi
from luigi import configuration
from luigi.contrib import redshift
from mortar.luigi import mortartask
from luigi.s3 import S3Target, S3PathTask

"""
This luigi pipeline builds an Amazon Redshift data warehouse from Wikipedia page view data stored in S3.

To run, ensure that you have setup your secure project configuration variables:

    mortar config:set HOST=<my-endpoint.redshift.amazonaws.com>
    mortar config:set PORT=5439
    mortar config:set DATABASE=<my-database-name>
    mortar config:set USERNAME=<my-master-username>
    mortar config:set PASSWORD=<my-master-username-password>

TaskOrder:
    ExtractWikipediaDataTask
    TransformWikipediaDataTask
    CopyToRedshiftTask
    ShutdownClusters

To run:
    mortar luigi luigiscripts/wikipedia-luigi.py \
        --input-base-path "s3://mortar-example-data/wikipedia/pagecounts-2011-07-aa" \
        --output-base-path "s3://<your-bucket-name>/wiki" \
        --table-name "pageviews"
"""

# REPLACE WITH YOUR PROJECT NAME
MORTAR_PROJECT = '<Your Project Name>'


def create_full_path(base_path, sub_path):
    """
    Helper function for constructing paths.
    """
    return '%s/%s' % (base_path, sub_path)


class WikipediaETLPigscriptTask(mortartask.MortarProjectPigscriptTask):
    """
    This is the base class for all of our Mortar related Luigi Tasks.  It extends
    the generic MortarProjectPigscriptTask to set common defaults we'll use
    for this pipeline: common data paths, and default cluster size.
    """

    # The base path to where input data is located.  In most cases your input data
    # should be stored in S3, however for testing purposes you can use a small
    # dataset that is stored in your Mortar project.
    input_base_path = luigi.Parameter()

    # The base path to where output data will be written.  This will be an S3 path.
    output_base_path = luigi.Parameter()

    # The cluster size to use for running Mortar jobs.  A cluster size of 0
    # will run in Mortar's local mode.  This is a fast (and free!) way to run jobs
    # on small data samples.  Cluster sizes >= 2 will run on a Hadoop cluster.
    cluster_size = luigi.IntParameter(default=5)

    def token_path(self):
        """
        Luigi manages dependencies between tasks by checking for the existence of
        files.  When one task finishes it writes out a 'token' file that will
        trigger the next task in the dependency graph.  This is the base path for
        where those tokens will be written.
        """
        return self.output_base_path

    def default_parallel(self):
        """
        This is used for an optimization that tells Hadoop how many reduce tasks should be used
        for a Hadoop job.  By default we'll tell Hadoop to use the number of reduce slots
        in the cluster.
        """
        if self.cluster_size - 1 > 0:
            return (self.cluster_size - 1) * mortartask.NUM_REDUCE_SLOTS_PER_MACHINE
        else:
            return 1

    def number_of_files(self):
        """
        This is used for an optimization when loading Redshift.  We can load Redshift faster by
        splitting the data to be loaded across multiple files.
        """
        if self.cluster_size - 1 > 0:
            return 2 * (self.cluster_size - 1) * mortartask.NUM_REDUCE_SLOTS_PER_MACHINE
        else:
            return 2


class ExtractWikipediaDataTask(WikipediaETLPigscriptTask):
    """
    This task runs the data extraction script pigscripts/01-wiki-extract-data.pig.
    """

    def requires(self):
        """
        The requires method is how you build your dependency graph in Luigi.  Luigi will not
        run this task until all tasks returned in this list are complete.

        ExtractWikipediaDataTask is the first task in our pipeline.  An empty list is
        returned to tell Luigi that this task is always ready to run.
        """
        return []

    def script_output(self):
        """
        The script_output method is how you define where the output from this task will be stored.

        Luigi will check this output location before starting any tasks that depend on this task.
        """
        return [S3Target(create_full_path(self.output_base_path, 'extract'))]

    def parameters(self):
        """
        This method defines the parameters that will be passed to Mortar when starting
        this pigscript.
        """
        return { 'OUTPUT_PATH': self.output_base_path,
                 'INPUT_PATH': self.input_base_path,
                }

    def script(self):
        """
        This is the name of the pigscript that will be run.

        You can find this script in the pigscripts directory of your Mortar project.
        """
        return '01-wiki-extract-data.pig'


class TransformWikipediaDataTask(WikipediaETLPigscriptTask):
    """
    This task runs the data transformation script pigscripts/02-wiki-transform-data.pig.
    """

    def requires(self):
        """
        Tell Luigi to run the ExtractWikipediaDataTask task before this task.
        """
        return [ExtractWikipediaDataTask(input_base_path=self.input_base_path,
                                         output_base_path=self.output_base_path)]

    def script_output(self):
        return [S3Target(create_full_path(self.output_base_path, 'transform'))]

    def parameters(self):
        return { 'OUTPUT_PATH': self.output_base_path,
                 'INPUT_PATH': self.input_base_path,
                 'REDSHIFT_PARALLELIZATION': self.number_of_files()
                }

    def script(self):
        return '02-wiki-transform-data.pig'


class CopyToRedshiftTask(redshift.S3CopyToTable):
    """
    This task copies data from S3 to Redshift.
    """

    # This is the Redshift table where the data will be written.
    table_name = luigi.Parameter()

    # As this task is writing to a Redshift table and not generating any output data
    # files, this S3 location is used to store a 'token' file indicating when the task has
    # been completed.
    output_base_path = luigi.Parameter()

    # This parameter is unused in this task, but it must be passed up the task dependency graph.
    input_base_path = luigi.Parameter()

    # The schema of the Redshift table where the data will be written.
    columns =[
        ('wiki_code', 'text'),
        ('language', 'text'),
        ('wiki_type', 'text'),
        ('article', 'varchar(max)'),
        ('day', 'int'),
        ('hour', 'int'),
        ('pageviews', 'int'),
        ('PRIMARY KEY', '(article, day, hour)')]

    def requires(self):
        """
        Tell Luigi to run the TransformWikipediaDataTask task before this task.
        """
        return [TransformWikipediaDataTask(input_base_path=self.input_base_path,
                                           output_base_path=self.output_base_path)]

    def redshift_credentials(self):
        """
        Returns a dictionary with the necessary fields for connecting to Redshift.
        """
        config = configuration.get_config()
        section = 'redshift'
        return {
            'host' : config.get(section, 'host'),
            'port' : config.get(section, 'port'),
            'database' : config.get(section, 'database'),
            'username' : config.get(section, 'username'),
            'password' : config.get(section, 'password'),
            'aws_access_key_id' : config.get(section, 'aws_access_key_id'),
            'aws_secret_access_key' : config.get(section, 'aws_secret_access_key')
        }

    def transform_path(self):
        """
        Helper function that returns the root directory where the transformed output
        has been stored.  This is the data that will be copied to Redshift.
        """
        return create_full_path(self.output_base_path, 'transform')

    def s3_load_path(self):
        """
        We want to load all files that begin with 'part' (the hadoop output file prefix) that
        came from the output of the transform step.
        """
        return create_full_path(self.transform_path(), 'part')

    """
    Property methods for connecting to Redshift.
    """

    @property
    def aws_access_key_id(self):
        return self.redshift_credentials()['aws_access_key_id']

    @property
    def aws_secret_access_key(self):
        return self.redshift_credentials()['aws_secret_access_key']

    @property
    def database(self):
        return self.redshift_credentials()['database']

    @property
    def user(self):
        return self.redshift_credentials()['username']

    @property
    def password(self):
        return self.redshift_credentials()['password']

    @property
    def host(self):
        return self.redshift_credentials()['host'] + ':' + self.redshift_credentials()['port']

    @property
    def table(self):
        return self.table_name

    @property
    def copy_options(self):
        '''Add extra copy options, for example:

         GZIP
         TIMEFORMAT 'auto'
         IGNOREHEADER 1
         TRUNCATECOLUMNS
         IGNOREBLANKLINES
        '''
        return 'GZIP'

    def table_attributes(self):
        '''Add extra table attributes, for example:

        DISTSTYLE KEY
        DISTKEY (MY_FIELD)
        SORTKEY (MY_FIELD_2, MY_FIELD_3)
        '''
        return 'DISTSTYLE EVEN'

class ShutdownClusters(mortartask.MortarClusterShutdownTask):
    """
    This is the very last task in the pipeline.  It will shut down all active
    clusters that are not currently running jobs.
    """

    # These parameters are not used by this task, but passed through for earlier tasks to use.
    input_base_path = luigi.Parameter()
    table_name = luigi.Parameter()

    # As this task is only shutting down clusters and not generating any output data,
    # this S3 location is used to store a 'token' file indicating when the task has
    # been completed.
    output_base_path = luigi.Parameter()

    def requires(self):
        """
        Tell Luigi that the CopyToRedshiftTask task needs to be completed
        before running this task.
        """
        return [CopyToRedshiftTask(input_base_path=self.input_base_path,
                                   output_base_path=self.output_base_path,
                                   table_name=self.table_name)]

    def output(self):
        return [S3Target(create_full_path(self.output_base_path, self.__class__.__name__))]


if __name__ == "__main__":
    """
    We tell Luigi to run the last task in the task dependency graph.  Luigi will then
    work backwards to find any tasks with its requirements met and start from there.

    The first time this pipeline is run the only task with its requirements met will be
    ExtractWikipediaDataTask which does not have any dependencies.
    """
    luigi.run(main_task_cls=ShutdownClusters)
