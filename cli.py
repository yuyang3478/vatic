import os
import sys
import math
import argparse
import config
import shutil
from turkic.cli import handler, importparser, Command, LoadCommand
from turkic.database import session
from vision import ffmpeg
import vision.visualize
import vision.track.interpolation
import turkic.models
from models import *
import cStringIO
import Image, ImageDraw, ImageFont

@handler("Decompresses an entire video into frames")
class extract(Command):
    def setup(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("video")
        parser.add_argument("output")
        parser.add_argument("--width", default=720, type=int)
        parser.add_argument("--height", default=480, type=int)
        parser.add_argument("--no-resize", action="store_true", default = False)
        parser.add_argument("--no-cleanup", action="store_true", default=False)
        return parser

    def __call__(self, args):
        try:
            os.makedirs(args.output)
        except:
            pass
        sequence = ffmpeg.extract(args.video)
        try:
            for frame, image in enumerate(sequence):
                if frame % 100 == 0:
                    print "Decoding frames {0} to {1}".format(frame, frame + 100)
                if not args.no_resize:
                    image.thumbnail((args.width, args.height), Image.BILINEAR)
                path = Video.getframepath(frame, args.output)
                try:
                    image.save(path)
                except IOError:
                    os.makedirs(os.path.dirname(path))
                    image.save(path)
        except:
            if not args.no_cleanup:
                print "Aborted. Cleaning up..."
                shutil.rmtree(args.output)
            raise

@handler("Imports a set of video frames")
class load(LoadCommand):
    def setup(self):
        parser = argparse.ArgumentParser(parents = [importparser])
        parser.add_argument("slug")
        parser.add_argument("location")
        parser.add_argument("labels", nargs="+")
        parser.add_argument("--length", type=int, default = 300)
        parser.add_argument("--overlap", type=int, default = 20)
        parser.add_argument("--skip", type=int, default = 0)
        parser.add_argument("--per-object-bonus", type=float)
        parser.add_argument("--completion-bonus", type=float)
        parser.add_argument("--trainer")
        return parser

    def title(self, args):
        return "Video annotation"

    def description(self, args):
        return "Draw boxes around objects moving around in a video."

    def cost(self, args):
        return 0.05

    def duration(self, args):
        return 7200 * 3

    def keywords(self, args):
        return "video, annotation, computer, vision"

    def __call__(self, args, group):
        print "Checking integrity..."

        # read first frame to get sizes
        path = Video.getframepath(0, args.location)
        try:
            im = Image.open(path)
        except IOError:
            print "Cannot read {0}".format(path)
            return
        width, height = im.size

        print "Searching for last frame..."

        # search for last frame
        toplevel = max(int(x)
            for x in os.listdir(args.location))
        secondlevel = max(int(x)
            for x in os.listdir("{0}/{1}".format(args.location, toplevel)))
        maxframes = max(int(os.path.splitext(x)[0])
            for x in os.listdir("{0}/{1}/{2}"
            .format(args.location, toplevel, secondlevel)))

        print "Found {0} frames.".format(maxframes)

        # can we read the last frame?
        path = Video.getframepath(maxframes, args.location)
        try:
            im = Image.open(path)
        except IOError:
            print "Cannot read {0}".format(path)
            return

        # check last frame sizes
        if im.size[0] != width and im.size[1] != height:
            print "First frame dimensions differs from last frame"
            return

        if session.query(Video).filter(Video.slug == args.slug).count() > 0:
            print "Video {0} already exists!".format(args.slug)
            return

        # create video
        video = Video(slug = args.slug,
                        location = args.location, 
                        width = width,
                        height = height,
                        totalframes = maxframes,
                        skip = args.skip,
                        perobjectbonus = args.per_object_bonus,
                        completionbonus = args.completion_bonus)

        session.add(video)

        print "Binding labels..."

        # create labels
        for labeltext in args.labels:
            label = Label(text = labeltext)
            session.add(label)
            video.labels.append(label)

        print "Creating symbolic link..."
        symlink = "public/frames/{0}".format(video.slug)

        if session.query(Video).get(args.slug):
            print "Video {0} already exists!".format(args.slug)
            return

        if args.trainer:
            trainer = session.query(Video).get(args.trainer)
            if not trainer:
                print "Trainer {0} does not exist!".format(args.trainer)
                return
            if not trainer.istraining:
                print "Video {0} is not for training!".format(args.trainer)
                return
        else:
            trainer = None

        # create video
        video = Video(slug = args.slug,
                        location = args.location, 
                        width = width,
                        height = height,
                        totalframes = maxframes,
                        skip = args.skip,
                        perobjectbonus = args.per_object_bonus,
                        completionbonus = args.completion_bonus,
                        trainingvideo = trainer)
        session.add(video)

        print "Binding labels..."

        # create labels
        for labeltext in args.labels:
            label = Label(text = labeltext)
            session.add(label)
            video.labels.append(label)

        print "Creating symbolic link..."
        symlink = "public/frames/{0}".format(video.slug)
        try:
            os.remove(symlink)
        except:
            pass
        os.symlink(video.location, symlink)

        print "Creating segments..."
        # create shots and jobs
        for start in range(0, video.totalframes, args.length):
            stop = min(start + args.length + args.overlap + 1,
                        video.totalframes)
            segment = Segment(start = start,
                                stop = stop,
                                video = video)
            job = Job(segment = segment, group = group)
            session.add(segment)
            session.add(job)

        if args.per_object_bonus:
            group.schedules.append(
                PerObjectBonus(amount = args.per_object_bonus))
        if args.completion_bonus:
            group.schedules.append(
                CompletionBonus(amount = args.completion_bonus))

        session.add(group)
        session.commit()

        print "Video imported and ready for publication."

@handler("Sets a video as a training video")
class training(Command):
    def setup(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("slug")
        return parser

    def __call__(self, args):
        video = session.query(Video).get(args.slug)
        if not video:
            print "Video {0} not found!".format(video.slug)
        if video.istraining:
            print "Video {0} already training video!".format(video.slug)

        video.istraining = True
        for segment in video.segments:
            for job in segment.jobs:
                if job.published:
                    print ("Video {0} already has published "
                        "shots".format(video.slug))
                job.ready = False

        session.add(video)
        session.commit()

        print "Annotate ground truth at:"
        for segment in video.segments:
            print segment.jobs[0].offlineurl()

@handler("Deletes an already imported video")
class delete(Command):
    def setup(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("slug")
        parser.add_argument("--force", action="store_true", default=False)
        return parser

    def __call__(self, args):
        if session.query(Video).filter(Video.slug == args.slug).count() == 0:
            print "Video {0} does not exist!".format(args.slug)
            return

        video = session.query(Video).filter(Video.slug == args.slug).one()

        query = session.query(Path)
        query = query.join(Job)
        query = query.join(Segment)
        query = query.filter(Segment.videoslug == video.slug)
        numpaths = query.count()
        if numpaths and not args.force:
            print "Video has {0} paths. Use --force to delete.".format(numpaths)
            return

        session.delete(video)
        session.commit()

        print "Deleted video and associated data."

class DumpCommand(Command):
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("slug")
    parent.add_argument("--interpolate", "-i", action="store_true", default=False)
    parent.add_argument("--merge", "-m", action="store_true", default=False)

    class Tracklet(object):
        def __init__(self, label, boxes, workers):
            self.label = label
            self.boxes = sorted(boxes, key = lambda x: x.frame)
            self.workers = workers

    def getdata(self, args):
        response = []
        if session.query(Video).filter(Video.slug == args.slug).count() == 0:
            print "Video {0} does not exist!".format(args.slug)
            return
        video = session.query(Video).filter(Video.slug == args.slug).one()
        for segment in video.segments:
            for job in segment.jobs:
                worker = job.hit.workerid
                for path in job.paths:
                    tracklet = DumpCommand.Tracklet(path.label.text,
                                                    path.getboxes(),
                                                    [worker])
                    response.append(tracklet)

        if args.interpolate:
            interpolated = []
            for track in response:
                path = vision.track.interpolation.LinearFill(track.boxes)
                tracklet = DumpCommand.Tracklet(track.label, path, track.workers)
                interpolated.append(tracklet)
            response = interpolated

        return video, response

@handler("Highlights a video sequence")
class visualize(DumpCommand):
    def setup(self):
        parser = argparse.ArgumentParser(parents = [self.parent])
        parser.add_argument("output")
        return parser

    def __call__(self, args):
        print "Fetching data..."
        video, data = self.getdata(args)

        print "Processing {0} tracks...".format(len(data))
        paths = [x.boxes for x in data]
        
        print "Highlighting frames..."
        it = vision.visualize.highlight_paths(video, paths)
        it = self.augment(args, video, data, it)
        vision.visualize.save(it, lambda x: "{0}/{1}.jpg".format(args.output, x))

    def augment(self, args, video, data, frames):
        offset = 100
        for im, frame in frames:
            aug = Image.new(im.mode, (im.size[0], im.size[1] + offset))
            aug.paste("black")
            aug.paste(im, (0, 0))
            draw = ImageDraw.ImageDraw(aug)

            s = im.size[1]
            font = ImageFont.truetype("arial.ttf", 14)

            # extract some data
            workerids = set()
            sum = 0
            for track in data:
                if frame in (x.frame for x in track.boxes):
                    for worker in track.workers:
                        if worker not in workerids:
                            workerids.add(worker)
                    sum += 1
            ypos = s + 5
            for worker in workerids:
                draw.text((5, ypos), worker, fill="white", font = font)
                ypos += draw.textsize(worker, font = font)[1] + 3

            size = draw.textsize(video.slug, font = font)
            draw.text((im.size[0] - size[0] - 5, s + 5), video.slug, font = font)

            text = "{0} annotations".format(sum)
            numsize = draw.textsize(text, font = font)
            draw.text((im.size[0] - numsize[0] - 5, s + 5 + size[1] + 3), text, font = font)

            yield aug, frame

@handler("Dumps the tracking data")
class dump(DumpCommand):
    def setup(self):
        parser = argparse.ArgumentParser(parents = [self.parent])
        parser.add_argument("--output", "-o")
        parser.add_argument("--xml", "-x", action="store_true", default=False)
        parser.add_argument("--json", "-j", action="store_true", default=False)
        parser.add_argument("--matlab", "-ml", action="store_true", default=False)
        parser.add_argument("--pickle", "-p", action="store_true", default=False)
        return parser

    def __call__(self, args):
        if args.output:
            file = open(args.output, 'w')
        else:
            file = cStringIO.StringIO()

        video, data = self.getdata(args)

        if args.xml:
            self.dumpxml(file, data)
        elif args.json:
            self.dumpjson(file, data)
        elif args.matlab:
            self.dumpmatlab(file, data)
        elif args.pickle:
            self.dumppickle(file, data)
        else:
            self.dumptext(file, data)

        if args.output:
            file.close()
        else:
            sys.stdout.write(file.getvalue())

    def dumpmatlab(self, file, data):
        results = []
        for id, track in enumerate(data):
            for box in track.boxes:
                data = {}
                data['id'] = id
                data['xtl'] = box.xtl
                data['ytl'] = box.ytl
                data['xbr'] = box.xbr
                data['ybr'] = box.ybr
                data['frame'] = box.frame
                data['lost'] = box.lost
                data['occluded'] = box.occluded
                data['label'] = track.label
                results.append(data)

        from scipy.io import savemat as savematlab
        savematlab(file,
            {"annotations": results}, oned_as="row")

    def dumpxml(self, file, data):
        file.write("<annotations count=\"{0}\">\n".format(len(data)))
        for id, track in enumerate(data):
            file.write("\t<track id=\"{0}\" label=\"{1}\">\n".format(id, track.label))
            for box in track.boxes:
                file.write("\t\t<box frame=\"{0}\"".format(box.frame))
                file.write(" xtl=\"{0}\"".format(box.xtl))
                file.write(" ytl=\"{0}\"".format(box.ytl))
                file.write(" xbr=\"{0}\"".format(box.xbr))
                file.write(" ybr=\"{0}\"".format(box.ybr))
                file.write(" outside=\"{0}\"".format(box.lost))
                file.write(" occluded=\"{0}\" />\n".format(box.occluded))
            file.write("\t</track>\n")
        file.write("</annotations>\n")

    def dumpjson(self, file, data):
        annotations = {}
        for id, track in enumerate(data):
            result = {}
            result['label'] = track.label
            boxes = {}
            for box in track.boxes:
                boxdata = {}
                boxdata['xtl'] = box.xtl
                boxdata['ytl'] = box.ytl
                boxdata['xbr'] = box.xbr
                boxdata['ybr'] = box.ybr
                boxdata['outside'] = box.lost
                boxdata['occluded'] = box.occluded
                boxes[int(box.frame)] = boxdata
            result['boxes'] = boxes
            annotations[int(id)] = result

        import json
        json.dump(annotations, file)
        file.write("\n")

    def dumppickle(self, file, data):
        annotations = []
        for track in data:
            result = {}
            result['label'] = track.label
            result['boxes'] = track.boxes
            annotations.append(result)

        import pickle
        pickle.dump(annotations, file, protocol = 2)

    def dumptext(self, file, data):
        for id, track in enumerate(data):
            for box in track.boxes:
                file.write(str(id))
                file.write(" ")
                file.write(str(box.xtl))
                file.write(" ")
                file.write(str(box.ytl))
                file.write(" ")
                file.write(str(box.xbr))
                file.write(" ")
                file.write(str(box.ybr))
                file.write(" ")
                file.write(str(box.frame))
                file.write(" ")
                file.write(str(box.lost))
                file.write(" ")
                file.write(str(box.occluded))
                file.write(" \"")
                file.write(track.label)
                file.write("\"\n")

@handler("List all videos loaded")
class list(Command):
    def __call__(self, args):
        videos = session.query(Video)
        for video in videos:
            print video.slug
