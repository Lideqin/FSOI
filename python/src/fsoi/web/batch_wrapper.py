"""
This script is an entry point for an AWS Lambda function to download
H5 files from S3 and create plots using existing functions.
"""

import sys
import os
import json
import boto3
from fsoi.web.serverless_tools import hash_request, get_reference_id, create_response_body, \
    create_error_response_body, RequestDao, ApiGatewaySender


# List to hold errors and warnings encountered during processing
errors = []
warns = []


def handler(request):
    """
    Create a chart as a PNG based on the input parameters
    :param request: Contains request details (validated)
    :return: None - result will be sent via API Gateway Websocket Connection URL
    """
    try:
        # get the request hash
        hash_value = hash_request(request)

        # process the request
        response = process_request(request)

        # send the response to all listeners
        print(json.dumps(response))
        message_all_clients(hash_value, response)
    except Exception as e:
        print(e)
        ref_id = get_reference_id(request)
        errors.append('Request processing failed')
        errors.append('Reference ID: %s' % ref_id)
        update_all_clients(hash_request(request), 'FAIL', 'Request processing failed', 0)


def process_request(validated_request):
    """
    Create a chart as a PNG based on the input parameters
    :param validated_request: Contains all of the request details
    :return: JSON response
    """
    # empty the list of errors
    del errors[:]
    del warns[:]

    # compute the hash value and get a reference ID
    hash_value = hash_request(validated_request)
    reference_id = get_reference_id(validated_request)

    # print some debug information to the CloudWatch Logs
    print('Reference ID: %s' % reference_id)
    print('Request hash: %s' % hash_value)
    print('Request:\n%s' % json.dumps(validated_request))

    # track the progress percentage
    progress = 0
    progress_step = int(90/len(validated_request['centers'])/3)

    # download data from S3
    update_all_clients(hash_value, 'RUNNING', 'Accessing data objects', progress)
    objects = download_s3_objects(validated_request)
    progress += 5

    # analyze downloaded data to determine if there were any centers with no
    # data, and remove those centers (if any) from the request and add a warning
    center_counts = {}
    for object in objects:
        if object[1] not in center_counts:
            center_counts[object[1]] = 0
        if object[5]:
            center_counts[object[1]] += 1
    for center in center_counts:
        if center_counts[center] == 0:
            warns.append('No data available for %s' % center)
            validated_request['centers'].remove(center)

    # iterate over each of the requested centers
    key_list = []
    centers = validated_request['centers']
    for center in centers:
        validated_request['centers'] = [center]
        if not errors:
            prepare_working_dir(validated_request)
        if not errors:
            update_all_clients(hash_value, 'RUNNING', 'Processing bulk stats for %s' % center, progress)
            process_bulk_stats(validated_request)
            progress += progress_step
        if not errors:
            update_all_clients(hash_value, 'RUNNING', 'Processing FSOI summary for %s' % center, progress)
            process_fsoi_summary(validated_request)
            progress += progress_step
        if not errors:
            update_all_clients(hash_value, 'RUNNING', 'Storing images for %s' % center, progress)
            key_list += cache_summary_plots_in_s3(hash_value, validated_request)
            progress += progress_step

    # restore original list of centers
    validated_request['centers'] = centers

    # create the comparison summary plots
    if not errors:
        update_all_clients(hash_value, 'RUNNING', 'Creating comparison plots', progress)
        process_fsoi_compare(validated_request)
        progress += 3
        update_all_clients(hash_value, 'RUNNING', 'Storing comparison plots', progress)
        key_list += cache_compare_plots_in_s3(hash_value, validated_request)
        progress += 1

    # clean up the working directory
    clean_up(validated_request)

    # restore the request to its original value
    validated_request['centers'] = centers

    # handle success cases
    if not errors:
        update_all_clients(hash_value, 'SUCCESS', 'Done.', 100)
        return create_response_body(key_list, hash_value, warns)

    # handle error cases
    update_all_clients(hash_value, 'FAIL', 'Failed to process request', progress)
    print('Errors:\n%s' % ','.join(errors))
    print('Warnings:\n%s' % ','.join(warns))
    errors.append('Reference ID: ' + reference_id)

    return create_error_response_body(hash_value, errors, warns)


def update_all_clients(req_hash, status_id, message, progress):
    """
    Update the DB and send a message to all clients with a new status update
    :param req_hash: The request hash
    :param status_id: [PENDING|RUNNING|SUCCESS|FAIL]
    :param message: Free-form text to be displayed to user
    :param progress: Integer value 0-100; representing percent complete
    :return: None
    """
    # update the DB
    RequestDao.update_status(req_hash, status_id, message, progress)

    # get the latest from the db
    latest = RequestDao.get_request(req_hash)

    # remove the client connection URLs
    if 'connections' in latest:
        latest.pop('connections')

    # send a status message to all clients
    message_all_clients(req_hash, latest)


def message_all_clients(req_hash, message):
    """
    Send a message to all clients listening to the request
    :param req_hash: The request hash
    :param message: The message
    :return: Number of clients notified
    """
    req = RequestDao.get_request(req_hash)

    # make sure the message is a string and not a dictionary
    if isinstance(message, dict):
        message = json.dumps(message)

    # count the number of clients that were sent messages
    sent = 0

    # send all of the clients a message
    if 'connections' in req:
        for url in req['connections']:
            if ApiGatewaySender.send_message_to_ws_client(url, message):
                sent += 1

    # return the number of messages sent
    return sent


def prepare_working_dir(request):
    """
    Create all of the necessary empty directories
    :param request: {dict} A validated and sanitized request object
    :return: {bool} True=success; False=failure
    """
    try:
        root_dir = request['root_dir']

        required_dirs = [root_dir, root_dir+'/work', root_dir+'/data', root_dir+'/plots/summary',
                         root_dir+'/plots/compare/full', root_dir+'/plots/compare/rad',
                         root_dir+'/plots/compare/conv']
        for center in request['centers']:
            required_dirs.append(root_dir + '/plots/summary/' + center)

        for required_dir in required_dirs:
            if not os.path.exists(required_dir):
                os.makedirs(required_dir)
            elif os.path.isfile(required_dir):
                return False

        return True
    except Exception as e:
        errors.append('Error preparing working directory')
        print(e)
        return False


def clean_up(request):
    """
    Clean up the temporary working directory
    :param request: {dict} A validated and sanitized request object
    :return: None
    """
    import shutil

    root_dir = request['root_dir']

    if root_dir is not None and root_dir != '/':
        try:
            shutil.rmtree(root_dir)
        except FileNotFoundError:
            print('%s not found when cleaning up' % root_dir)


def download_s3_objects(request):
    """
    Download all required objects from S3
    :param request: {dict} A validated and sanitized request object
    :return: {list} A list of lists, where each item in the main list is an object that was expected
                    to be downloaded.  The sub lists contain [s3_key, center, norm, date, cycle,
                    downloaded_boolean]
    """
    try:
        bucket = os.environ['DATA_BUCKET']
        prefix = os.environ['OBJECT_PREFIX']
        data_dir = request['root_dir'] + '/data'

        s3 = boto3.client('s3')

        objs = get_s3_object_urls(request)
        s3msgs = []
        all_data_missing = True
        for obj in objs:
            key = obj[0]

            # create the local file name
            local_dir = data_dir+'/'+key[:key.rfind('/')]+'/'
            local_file = key[key.rfind('/')+1:]

            # create the local directory if needed
            if not os.path.exists(local_dir):
                os.makedirs(local_dir)

            # check to see if we already have the file
            if os.path.exists(local_dir + local_file):
                obj.append(True)
                continue

            # download the file from S3
            try:
                print('Downloading S3 object: s3://%s/%s/%s' % (bucket, prefix, key))
                s3.download_file(Bucket=bucket, Key=prefix+'/'+key, Filename=local_dir+local_file)
                if not os.path.exists(local_dir + local_file):
                    print('Could not download S3 object: s3://%s/%s/%s' % (bucket, prefix, key))
                    obj.append(False)
                else:
                    all_data_missing = False
                    obj.append(True)
            except Exception as e:
                tokens = key.split('.')
                center = tokens[0]
                date = tokens[2][0:8]
                cycle = tokens[2][8:]
                s3msgs.append('Missing data: %s %s %sZ' % (center, date, cycle))
                obj.append(False)
                print(e)

        # put the S3 download messages either into errors or warns
        for msg in s3msgs:
            if all_data_missing:
                errors.append(msg)
            else:
                warns.append(msg)

        return objs
    except Exception as e:
        errors.append('Error downloading data object from S3')
        print(e)
        return objs


def process_bulk_stats(request):
    """
    Run the summary_bulk.py script on the data we have downloaded
    :param request: {dict} A validated and sanitized request object
    :return: None
    """
    from fsoi.stats.summary_bulk import summary_bulk_main

    try:
        # delete previous work file
        work_file = request['root_dir'] + 'work/' + request['centers'][0] + '/dry/bulk_stats.h5'
        if os.path.exists(work_file):
            os.remove(work_file)

        sys.argv = ['script',
                    '--center',
                    ','.join(request['centers']),
                    '--norm',
                    request['norm'],
                    '--rootdir',
                    request['root_dir'],
                    '--begin_date',
                    request['start_date'] + request['cycles'][0],
                    '--end_date',
                    request['end_date'] + request['cycles'][-1],
                    '--interval',
                    str(request['interval'])]

        print('running summary_bulk_main: %s' % ' '.join(sys.argv))
        summary_bulk_main()
    except Exception as e:
        errors.append('Error computing bulk statistics')
        print(e)


def process_fsoi_summary(request):
    """
    Run the fsoi_summary.py script on the bulk statistics
    :param request: {dict} A validated and sanitized request object
    :return: None
    """
    from fsoi.plots.summary_fsoi import summary_fsoi_main

    try:
        sys.argv = [
            'script',
            '--center',
            request['centers'][0],
            '--norm',
            request['norm'],
            '--rootdir',
            request['root_dir'],
            '--platform',
            request['platforms'],
            '--savefigure',
            '--cycle'
        ]
        for cycle in request['cycles']:
            sys.argv.append(cycle)

        print('running summary_fsoi_main: %s' % ' '.join(sys.argv))
        summary_fsoi_main()
    except Exception as e:
        errors.append('Error computing FSOI summary')
        print(e)


def process_fsoi_compare(request):
    """
    Run the compare_fsoi.py script on the final statistics
    :param request:
    :return:
    """
    from fsoi.plots.compare_fsoi import compare_fsoi_main

    try:
        sys.argv = [
            'script',
            '--rootdir',
            request['root_dir'],
            '--centers']
        sys.argv += request['centers']
        sys.argv += [
            '--norm',
            request['norm'],
            '--savefigure',
            '--cycle'
        ]
        sys.argv += request['cycles']

        print('running compare_fsoi_main: %s' % ' '.join(sys.argv))
        compare_fsoi_main()
    except Exception as e:
        errors.append('Error creating FSOI comparison plots')
        print(e)


def dates_in_range(start_date, end_date):
    """
    Get a list of dates in the range
    :param start_date: {str} yyyyMMdd
    :param end_date:  {str} yyyyMMdd
    :return: {list} List of dates in the given range (inclusive)
    """
    from datetime import datetime as dt

    start_year = int(start_date[0:4])
    start_month = int(start_date[4:6])
    start_day = int(start_date[6:8])
    start = dt(start_year, start_month, start_day)
    s = int(start.timestamp())

    end_year = int(end_date[0:4])
    end_month = int(end_date[4:6])
    end_day = int(end_date[6:8])
    end = dt(end_year, end_month, end_day)
    e = int(end.timestamp())

    dates = []
    for ts in range(s, e + 1, 86400):
        d = dt.utcfromtimestamp(ts)
        dates.append('%04d%02d%02d' % (d.year, d.month, d.day))

    return dates


def get_s3_object_urls(request):
    """
    Get a list of the S3 object URLs required to complete this request
    :param request: {dict} A validated and sanitized request object
    :return: {list} A list of objects expected to be downloaded, where each item in the list is
                    a list containing [s3_key, center, norm, date, cycle].
    """
    start_date = request['start_date']
    end_date = request['end_date']
    centers = request['centers']
    norm = request['norm']
    cycles = request['cycles']

    s3_objects = []
    for date in dates_in_range(start_date, end_date):
        for center in centers:
            for cycle in cycles:
                s3_objects.append([
                    '%s/%s.%s.%s%s.h5' % (center, center, norm, date, cycle),
                    center,
                    norm,
                    date,
                    cycle]
                )

    return s3_objects


def cache_compare_plots_in_s3(hash_value, request):
    """
    Copy all of the new comparison plots to S3
    :param hash_value: {str} The hash value of the request
    :param request: {dict} The full request
    :return: None
    """
    # retrieve relevant environment variables
    bucket = os.environ['CACHE_BUCKET']
    root_dir = os.environ['FSOI_ROOT_DIR']
    img_dir = root_dir + '/plots/compare/full'

    # list of files to cache
    files = [
        img_dir + '/ImpPerOb___CYCLE__.png',
        img_dir + '/FracImp___CYCLE__.png',
        img_dir + '/ObCnt___CYCLE__.png',
        img_dir + '/TotImp___CYCLE__.png',
        img_dir + '/FracNeuObs___CYCLE__.png',
        img_dir + '/FracBenObs___CYCLE__.png'
    ]

    # create the s3 client
    s3 = boto3.client('s3')

    # create the cycle identifier
    cycle = ''
    for c in request['cycles']:
        cycle += '%02dZ' % int(c)

    # loop through all centers and files
    key_list = []
    for file in files:
        # replace the center in the file name
        filename = file.replace('__CYCLE__', cycle)
        if os.path.exists(filename):
            print('Uploading %s to S3...' % filename)
            key = hash_value + '/comparefull_' + filename[filename.rfind('/') + 1:]
            s3.upload_file(Filename=filename, Bucket=bucket, Key=key)
            key_list.append(key)

    return key_list


def cache_summary_plots_in_s3(hash_value, request):
    """
    Copy all of the new summary plots to S3
    :param hash_value: {str} The hash value of the request
    :param request: {dict} The full request
    :return: None
    """
    # retrieve relevant environment variables
    bucket = os.environ['CACHE_BUCKET']
    root_dir = os.environ['FSOI_ROOT_DIR']
    img_dir = root_dir + '/plots/summary'

    # list of files to cache
    files = [
        img_dir + '/__CENTER__/__CENTER___ImpPerOb___CYCLE__.png',
        img_dir + '/__CENTER__/__CENTER___FracImp___CYCLE__.png',
        img_dir + '/__CENTER__/__CENTER___ObCnt___CYCLE__.png',
        img_dir + '/__CENTER__/__CENTER___TotImp___CYCLE__.png',
        img_dir + '/__CENTER__/__CENTER___FracNeuObs___CYCLE__.png',
        img_dir + '/__CENTER__/__CENTER___FracBenObs___CYCLE__.png'
    ]

    # create the s3 client
    s3 = boto3.client('s3')

    # create the cycle identifier
    cycle = ''
    for c in request['cycles']:
        cycle += '%02dZ' % int(c)

    # loop through all centers and files
    key_list = []
    for center in request['centers']:
        for file in files:
            # replace the center in the file name
            filename = file.replace('__CENTER__', center).replace('__CYCLE__', cycle)
            if os.path.exists(filename):
                print('Uploading %s to S3...' % filename)
                key = hash_value + '/' + filename[filename.rfind('/') + 1:]
                s3.upload_file(Filename=filename, Bucket=bucket, Key=key)
                key_list.append(key)

    if not key_list:
        errors.append('Failed to generate plots')

    return key_list


def main():
    """
    Main function from the command line
    :return: None
    """
    print('args:')
    for arg in sys.argv:
        print(arg)
    global_request = json.loads(sys.argv[1])
    handler(global_request)


if __name__ == '__main__':
    main()
