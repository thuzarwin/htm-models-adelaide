#!/usr/bin/env python2.7
from calendar import monthrange
from collections import Counter
from wsgiref.simple_server import make_server
import numpy
from pluck import pluck
from pyramid.config import Configurator
from pyramid.renderers import render_to_response
from datetime import datetime, timedelta, date
import pymongo
import os
from pyramid.view import view_config
import yaml
import json
from bson import json_util
from geopy.distance import vincenty

from pyramid.events import subscriber
from pyramid.events import BeforeRender


REPORTS = ('Daily Total', 'Monthly Average', 'AM Peak', 'PM Peak',
           'Highest Peak Volumes', 'Highest AM Peaks',
           'Highest PM Peaks', 'Phase Splits',
           'Degree of Saturation', 'VO VK Ratio')
fmt = '%d/%m/%Y'


def get_site_dir():
    return os.path.dirname(os.path.realpath(__file__))

try:
    path = os.path.join(os.path.dirname(get_site_dir()), '../connection.yaml')
    
    print "looking in path", path
    with open(path, 'r') as f:
        conf = yaml.load(f)
        mongo_uri = conf['mongo_uri']
        mongo_database = conf['mongo_database']
        mongo_collection = conf['mongo_collection']
        gmaps_key = conf['GMAPS_API_KEY']
        max_vehicles = conf['max_vehicles']

except:
    raise Exception('No connection.yaml with mongo_uri defined! please make one with a mongo_uri variable')


@subscriber(BeforeRender)
def add_global(event):
    event['GMAPS_API_KEY'] = gmaps_key
    event['date_format'] = '%Y-%m-%d %H:%M'
    event['reports'] = REPORTS
    event['max_vehicles'] = max_vehicles


def _get_mongo_client():
    """
    Give you a pymongoclient for the database
    might need to do some caching or we'll end up
    with loads of open connections
    :return: a pymongo client
    """
    return pymongo.MongoClient(mongo_uri)


def _get_intersection(intersection):
    """
    Get information for a single intersection
    :return: a dict with the info
    """
    with _get_mongo_client() as client:
        coll = client[mongo_database]['locations']
        return coll.find_one({'intersection_number': intersection})


def _get_neighbours(intersection):
    """

    :param intersection:
    :return:
    """
    center = _get_intersection(intersection)
    if not center or 'neighbours' not in center or len(center['neighbours']) == 0:
        return []
    with _get_mongo_client() as client:
        coll = client[mongo_database]['locations']
        if type(center['neighbours']) is dict:
            neighbours = center['neighbours'].keys()
        else:
            neighbours = center['neighbours']
        return list(coll.find({'intersection_number': {'$in': neighbours}}))


def _get_intersections(images=True):
    """
    Get the signalised intersections for Adelaide
    :return: a cursor of documents of signalised intersections
    """
    with _get_mongo_client() as client:
        coll = client[mongo_database]['locations']
        exclude = {'_id': False}
        if not images:
            exclude['scats_diagram'] = False
        return coll.find({'intersection_number': {'$exists': True}}, exclude)


def get_accident_near(time_start, time_end, intersection, radius=150):
    """
    Return any accidents at this time,
    should probably be listed in the app
    :param time:
    :param intersection:
    :return:
    """
    with _get_mongo_client() as client:
        db = client[mongo_database]
        crashes = db['crashes']
        locations = db['locations']
        location = locations.find_one({'intersection_number': intersection})
        # timestamp = datetime.utcfromtimestamp(float(time))
        # delta = timedelta(minutes=30)
        query = {
            'loc': {
                '$geoNear': {
                   '$geometry': location['loc'],
                   '$maxDistance': radius
                }
            },
        }
        if time_start is not None:
            query['datetime'] = {
                '$gte': time_start,
                '$lte': time_end
            }

        return list(crashes.find(query).sort('datetime', pymongo.ASCENDING)), radius


def get_anomaly_scores(from_date=None, to_date=None, intersection='3001', anomaly_threshold=None):
    """

    :param from_date: unix time to get readings from
    :param to_date:  unix time to get readings until
    :param intersection:  the intersection to get readings for
    :return:
    """
    if type(from_date) is int:
        from_date = du(from_date)
    if type(to_date) is int:
        to_date = du(to_date)
    if from_date > to_date:
        from_date, to_date = to_date, from_date
    with _get_mongo_client() as client:
        # input is a unix date
        coll = client[mongo_database][mongo_collection]
        query = {'site_no': intersection}
        if from_date or to_date:
            query['datetime'] = {}
        if from_date is not None:
            query['datetime']['$gte'] = from_date
        if to_date is not None:
            query['datetime']['$lte'] = to_date
        if anomaly_threshold is not None:
            query['anomaly_score'] = {'$gte': float(anomaly_threshold)}
        return coll.find(query, {'_id':0,'predictions':0}).sort('datetime', pymongo.ASCENDING)


def _get_daily_volume(data, hour=None):
    """

    :param data: the data
    :param hour:  hour to take the volume for ?
    :return: a counter of day: volume
    """
    counter = Counter()
    for i in data:
        if hour and i['datetime'].hour != hour:
            continue
        for s, c in i['readings'].items():
            if c < max_vehicles:
                counter[i['datetime'].date()] += c
    return counter

def intersection_info(request):
    request.response.content_type = 'application/json'
    return _get_intersection(request.matchdict['intersection'])

def _monthly_average(data):
    monthly_average = Counter()
    for i, j in _get_daily_volume(data).items():
        monthly_average[date(i.year, i.month, 1)] += j
    for i in monthly_average:
        monthly_average[i] /= monthrange(i.year, i.month)[1]
    return sorted(monthly_average.items())


def _daily_volume(data):
    return sorted(_get_daily_volume(data).items())


def _am_peak(data):
    return sorted(_get_daily_volume(data, 8).items())


def _pm_peak(data):
    return sorted(_get_daily_volume(data, 15).items())


def _highest_peak_volumes(data):
    return sorted(_get_daily_volume(data).most_common(30), key=lambda x: x[1], reverse=True)


def _highest_am_peaks(data):
    return sorted(_get_daily_volume(data, 8).most_common(30), key=lambda x: x[1], reverse=True)


def _highest_pm_peaks(data):
    return sorted(_get_daily_volume(data, 15).most_common(30), key=lambda x: x[1], reverse=True)


def _phase_splits(data):
    return []


def _saturation_degree(data):
    return []


def _vo_vk_ratio(data):
    return []


def _get_report(intersection, report, start=None, end=None):
    """
    Return a report
    :param intersection:
    :param report:
    :param start:
    :param end:
    :return:
    """

    report_funcs = dict(zip(map(lambda x: x.lower().replace(' ', '_'), REPORTS),
                            [_daily_volume, _monthly_average, _am_peak, _pm_peak, _highest_peak_volumes,
                             _highest_am_peaks, _highest_pm_peaks, _phase_splits,
                             _saturation_degree, _vo_vk_ratio]))
    if report not in report_funcs:
        return "No such report format exists"
    if start is None and end is not None:
        # start will be 1 year before end
        start = end - timedelta(days=365)
    elif end is None and start is not None:
        end = start + timedelta(days=365)
    with _get_mongo_client() as client:
        coll = client[mongo_database][mongo_collection]
        query = {'site_no': intersection}
        if start and end:
            query['datetime'] = {'$gte': start, '$lte': end}
        data = coll.find(query)
        return report_funcs[report](data), intersection, start, end


def show_report(request):
    """
    :param request:
    :return:
    """
    args = request.matchdict
    start, end = None, None
    if 'start' in request.GET:
        start = datetime.strptime(request.GET['start'], fmt)
    if 'end' in request.GET:
        end = datetime.strptime(request.GET['end'], fmt)
    data, intersection, start, end = _get_report(args['intersection'], args['report'], start, end)
    if len(data):
        arr = numpy.array([i[1] for i in data])
        stats = {'Standard Deviation': numpy.std(arr), 'Average': numpy.average(arr)}
    else:
        stats = "Error"
    return render_to_response(
        'views/report.mak',
        {'data': data,
         'report': args['report'],
         'intersection': intersection,
         'stats': stats,
         'start': start,
         'end': end},
        request
    )


def show_map(request):
    """

    :param request:
    :return:
    """
    intersections = _get_intersections(images=True)
    return render_to_response(
        'views/map.mako',
        {'intersections': json.dumps(list(intersections))
         },
        request=request
    )


def get_readings_anomaly_json(request):
    """

    :param request:
    :return:
    """
    
    args = request.matchdict
    request.response.content_type = 'application/json'
    ft = request.GET.get('from',None)
    tt = request.GET.get('to',None)
    if ft is not None:
        ft = int(ft)
    if tt is not None:
        tt = int(tt)
    return get_anomaly_scores(ft, tt, args['intersection'])


def intersections_json():
    """

    :return:
    """
    return list(_get_intersections())


def list_intersections(request):
    """

    :param request:
    :return:
    """
    return render_to_response(
        'views/list.mak',
        {'intersections': _get_intersections(),
         'reports': REPORTS
         },
        request=request
    )


def get_diagrams(intersections):
    with _get_mongo_client() as client:
        db = client[mongo_database]
        intersections = pluck(intersections, 'intersection_number')
        return db['locations'].find({'intersection_number': {'$in': intersections}, 'scats_diagram': {'$exists': True}})


def show_intersection(request):
    """
    :param request:
    :return:
    """

    args = request.matchdict
    # show specific intersection if it exists
    site = args['site_no']
    
    intersection = _get_intersection(site)
    if intersection is None:
        return render_to_response('views/missing_intersection.mako', {},  request)
    # if 'neighbours' in intersection:
    #     intersection['_neighbours'] = intersection['neighbours']
    intersection['neighbours'] = _get_neighbours(site)

    cursor = get_anomaly_scores(intersection=site)
    day_range = 60
    #get the very latest date
    if cursor.count() == 0:
        anomaly_score_count = 0
    else:
        last = cursor[cursor.count()-1]['datetime']
        
        cursor = get_anomaly_scores(last - timedelta(days=day_range), last, intersection=site)
        anomaly_score_count = cursor.count()
    if anomaly_score_count == 0:
        time_start = None
        time_end = None
    else:
        time_start = cursor[0]['datetime']
        time_end = cursor[anomaly_score_count - 1]['datetime']
    try:
        intersection['sensors'] = intersection['sensors']
    except:
        intersection['sensors'] = 'Unknown'
    incidents, radius = get_accident_near(time_start, time_end, intersection['intersection_number'])
    neighbour_diagrams = get_diagrams(intersection['neighbours'])
    if 'neighbours_sensors' not in intersection:
        intersection['neighbours_sensors'] = {k['intersection_number']:{'to':[],'from':[]} for k in intersection['neighbours']}
    return render_to_response(
        'views/intersection.mako',
        {'intersection': intersection,
         'scores_count': anomaly_score_count,
         'incidents': incidents,
         'radius': radius,
         'day_range': day_range,
         'time_start': time_start,
         'time_end': time_end,
         'scats_diagrams': neighbour_diagrams
         },
        request=request
    )


def du(unix):
    return datetime.utcfromtimestamp(float(unix))


def get_accident_near_json(request):
    args = request.matchdict
    request.response.content_type = 'application/json'
    intersection, time_start, time_end = args['intersection'], du(args['time_start']), du(args['time_end'])
    radius = int(args['radius'])
    return get_accident_near(time_start, time_end, intersection, radius)


def validate_nodes(nodes):
    if len(nodes) == 0:
        return True 
    with _get_mongo_client() as client:
        locs = client[mongo_database]['locations']
        ns = locs.find({'intersection_number': {'$in': nodes}})
        return ns.count() == len(nodes)

@view_config(route_name='update_neighbours', renderer='json', request_method='POST')
def update_neighbours(request):
    with _get_mongo_client() as client:
        locs = client[mongo_database]['locations']
        data = request.json_body
        # print data
        if not validate_nodes(data.keys()):
            return {'error': 'Invalid nodes'}
        locs.update_one({'intersection_number': request.matchdict['site_no']},
                        {
                            '$set': {
                                'neighbours_sensors': data
                            }
                        })
        return {'success': True}

@view_config(route_name='update_neighbour_list', renderer='json', request_method='POST')
def update_neighbour_list(request):
    with _get_mongo_client() as client:
        locs = client[mongo_database]['locations']
        data = request.POST['neighbours'].split(',')
        # print data
        # print data
        if not validate_nodes(data):
            return {'error': 'Invalid nodes'}
        # return
        locs.update_one({'intersection_number': request.matchdict['site_no']},
                        {
                            '$set': {
                                'neighbours': data
                            }
                        })
        return {'success': True}


@view_config(route_name='crash_investigate', renderer='views/crash.mako', request_method='GET')
def investigate_crash(request):
    intersections = _get_intersections()
    return {
        'intersections': json.dumps(list(intersections))
    }


@view_config(route_name='crash_investigate', renderer='bson', request_method='POST')
def crash_in_polygon(request):
    with _get_mongo_client() as client:
        crashes_collection = client[mongo_database]['crashes']

        points = request.json_body
        # make sure it's a list of lists of floats
        points.append(points[0])
        crashes = crashes_collection.find({'loc': {
            '$geoWithin': {
                '$geometry': {
                    'type': 'Polygon',
                    'coordinates': [points]
                }
            }
        }})
        readings_collection = client[mongo_database]['readings']
        crashes = list(crashes)
        td = timedelta(minutes=5)
        for i, crash in enumerate(crashes):
            # find the nearest 2 intersections
            # and get the readings for the downstream one
            sites = client[mongo_database]['locations'].find({
                'loc': {
                    '$geoNear': {
                        '$geometry': crash['loc']
                    }
                }
            }).limit(2)
            sites = pluck(list(sites), 'intersection_number')
            readings = readings_collection.find({
                'datetime': {'$gte': crash['datetime'] - td, '$lte': crash['datetime'] + td},
                'site_no': {'$in': sites}
            }).limit(6).sort([['site_no', pymongo.ASCENDING], ['datetime', pymongo.ASCENDING]])
            crashes[i]['readings'] = list(readings)
            crashes[i]['sites'] = sites
    return {
        'crashes': crashes
    }


def show_incidents(request):
    """
    Show the
    :param request:
    :return:
    """
    with _get_mongo_client( ) as client:
        cursor = client[mongo_database][mongo_collection].find().sort('datetime')
        start = cursor[0]['datetime']
        end = cursor[cursor.count()-1]['datetime']
        incidents = []
        # get incidents in this range in CITY OF ADELAIDE lga
    
        crashes = client[mongo_database]['crashes']
        results = crashes.find({'datetime':{'$gte': start, '$lte': end}, 'LGA_Name': 'CITY OF ADELAIDE'})
        # get the readings of the nearest intersection at at the nearest time step
        td = timedelta(minutes=5)
        for crash in results:
            site = client[mongo_database]['locations'].find_one({
                'loc': {
                    '$geoNear': {
                       '$geometry': crash['loc']
                    }
                }
            })
            readings = client[mongo_database][mongo_collection].find({
                'datetime': {'$gte': crash['datetime']- td,'$lte': crash['datetime'] + td},
                'site_no': site['intersection_number']
            }).limit(3).sort('datetime')
            incidents.append((crash, list(readings), site, vincenty(site['loc']['coordinates'], crash['loc']['coordinates']).meters))
  #  print json.dumps(incidents, indent=4, default=json_util.default)
    return render_to_response(
        'views/incidents.mak',
        {'incidents': incidents},
        request=request
    )

def main(global_config, **settings):
    config = Configurator()
    config.include('pyramid_mako')
    config.include('pyramid_debugtoolbar')
    config.add_renderer('bson', 'htmsite.renderers.BSONRenderer')
    config.add_renderer('pymongo_cursor', 'htmsite.renderers.PymongoCursorRenderer')
    config.add_route('map', '/')
    config.add_view(show_map, route_name='map')
    config.add_route('intersection', '/intersection/{site_no}')
    config.add_route('intersection_json', '/intersections.json')
    config.add_route('readings_anomaly_json', '/get_readings_anomaly_{intersection}.json')
    config.add_view(get_readings_anomaly_json, route_name='readings_anomaly_json', renderer='pymongo_cursor')
    config.add_view(intersections_json, route_name='intersection_json', renderer='json')
    config.add_view(show_intersection, route_name='intersection')
    config.add_route('intersection_info', '/intersection_{intersection}.json')
    config.add_view(intersection_info, route_name='intersection_info', renderer='bson')
    config.add_route('update_neighbours', '/intersection/{site_no}/update_neighbours')
    config.add_route('update_neighbour_list', '/intersection/{site_no}/update_neighbours_list')
    config.add_route('intersections', '/intersections')
    config.add_route('reports', '/reports/{intersection}/{report}')
    config.add_route('accidents', '/accidents/{intersection}/{time_start}/{time_end}/{radius}')
    config.add_route('crash_investigate', '/crashes')
    config.add_view(get_accident_near_json, route_name='accidents', renderer='bson')
    config.add_view(show_report, route_name='reports')
    config.add_view(list_intersections, route_name='intersections')

    config.add_route('incidents', '/incidents')
    config.add_view(show_incidents, route_name='incidents')
    config.add_static_view(name='assets', path='assets', cache_max_age=3600)
    config.scan()
    return config.make_wsgi_app()
