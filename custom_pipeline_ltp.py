from __future__ import print_function
import json
import sys
from cluster_helper.cluster import cluster_view
sys.path.append('/home1/maint/event_creation')
from event_creation.submission.convenience import run_session_import
from event_creation.submission.tasks import IndexAggregatorTask


##########
#
# Input builders:
#
##########
def build_inputs(exp, subj, sess):
    inputs = dict(
        protocol='ltp',
        subject=subj,
        montage='0.0',
        montage_num='0',
        localization='0',
        experiment=exp,
        new_experiment=exp,
        ram_experiment='RAM_%s' % exp,
        #force=False,
        force_events=True,
        force_eeg=False,
        do_compare=False,
        code=subj,
        session=sess,
        original_session=sess,
        groups=('ltp',),
        attempt_import=False,
        attempt_conversion=False,
        PS4=False
    )
    return inputs

##########
#
# Event creation management
#
##########
def automatic_event_creator(experiment, check_index=True):

    inputs = []
    exp = experiment
    try:
        # Get dictionary of new/recently modified sessions
        with open('custom_run.json', 'r') as f:
            new_sess = json.load(f)
        # Get index for ltp database to identify which sessions have been proccessed
        with open('/protocols/ltp.json', 'r') as f:
            db_index = json.load(f)
            if db_index != {}:
                db_index = db_index['protocols']['ltp']['subjects']
    except IOError:
        print('Unable to load necessary session information for experiment: ', exp)
        print('Skipping...')
    print(new_sess)
    # Create an input structure for each new session that has not been processed
    for subject in new_sess:
        for session in new_sess[subject]:
            if check_index:
                print('checking index')
                if (db_index == {}) or (subject not in db_index) or (exp not in db_index[subject]['experiments']) or (str(session) not in db_index[subject]['experiments'][exp]['sessions']):
                    inputs.append(build_inputs(exp, subject, session))
                    print('Session to run: ', exp, subject, session)
            else:
                inputs.append(build_inputs(exp, subject, session))
                print('Session to run: ', exp, subject, session)

    # Submit a job to the cluster for each session that needs to be processed
    n_jobs = len(inputs)
    #for inp in inputs:
    #    run_session_import(inp)
    print(n_jobs, " jobs")
    if n_jobs > 0:
        with cluster_view(scheduler='sge', queue='RAM.q', num_jobs=n_jobs, cores_per_job=6) as view:
            view.map(run_session_import, inputs)


if __name__=='__main__':
    try:
        automatic_event_creator(experiment="NiclsCourierReadOnly",
                check_index=False)
    except Exception as e:
        print("failed")
        print(e)
        pass
    IndexAggregatorTask().run(protocols='ltp')
