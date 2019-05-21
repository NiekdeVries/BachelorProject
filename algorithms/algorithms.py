import cv2
import numpy as np
import slic
from os import listdir
import maxflow
import matplotlib.pyplot as plt
from skimage.segmentation import mark_boundaries
from skimage.segmentation import find_boundaries
from sklearn.cluster import KMeans


class Algorithms:
    def __init__(self, image_paths):
        self.images = image_paths
        self.imgs_float64 = dict()
        self.imgs_segmentation = dict()
        self.imgs_segment_ids = dict()
        self.imgs_segment_neighbors = dict()
        self.imgs_segment_histograms_hsv = dict()
        self.imgs_segment_histograms_hsv_normalized = dict()
        self.imgs_histograms_hsv = dict()
        self.imgs_foreground_segments = dict.fromkeys(image_paths, [])
        self.imgs_background_segments = dict.fromkeys(image_paths, [])
        self.imgs_cosegmented = dict()

    # generate super-pixel segments for all images using SLIC
    def compute_superpixels_slic(self, num_segments, compactness=10.0, max_iter=10, sigma=0):
        for image in self.images:
            self.imgs_float64[image] = slic.read_image_as_float64(image)
            self.imgs_segmentation[image] = slic.get_segmented_image(self.imgs_float64[image], num_segments,
                                                                     compactness, max_iter, sigma)
            self.imgs_segment_ids[image] = np.unique(self.imgs_segmentation[image])

    # generate hsv histograms for every segment in all images
    # also generates normalized versions
    def compute_histograms_hsv(self, bins_H=20, bins_S=20):
        for img in self.images:
            hsv = cv2.cvtColor(self.imgs_float64[img].astype('float32'), cv2.COLOR_BGR2HSV)
            self.imgs_histograms_hsv[img] = np.float32(cv2.calcHist([hsv], [0, 1], None, [bins_H, bins_S], [0, 360, 0, 1]))

            self.imgs_segment_histograms_hsv[img] = \
                np.float32([cv2.calcHist([hsv], [0, 1], np.uint8(self.imgs_segmentation[img] == i), [bins_H, bins_S],
                                         [0, 360, 0, 1]) for i in self.imgs_segment_ids[img]])

            self.imgs_segment_histograms_hsv_normalized[img] = np.float32([h / h.flatten().sum() for h in self.imgs_segment_histograms_hsv[img]])

    # Shows a plot of the histogram of the entire image at image_path or one of its segments
    def show_histogram(self, image_path, segment=None):
        if segment is None:
            plt.title(image_path.split('/')[-1])
            plt.imshow(self.imgs_histograms_hsv[image_path], interpolation='nearest')
        else:
            plt.title(image_path.split('/')[-1] + '    segment ' + str(segment))
            plt.imshow(self.imgs_segment_histograms_hsv[image_path][segment], interpolation='nearest')
        plt.xlabel('Saturation bins')
        plt.ylabel('Hue bins')
        plt.show()
        plt.clf()

    # compute the neighbor segments of each segment
    def compute_neighbors(self):
        for img in self.images:
            vs_right = np.vstack([self.imgs_segmentation[img][:, :-1].ravel(), self.imgs_segmentation[img][:, 1:].ravel()])
            vs_below = np.vstack([self.imgs_segmentation[img][:-1, :].ravel(), self.imgs_segmentation[img][1:, :].ravel()])
            neighbor_edges = np.unique(np.hstack([vs_right, vs_below]), axis=1)
            self.imgs_segment_neighbors[img] = [[] for x in self.imgs_segment_ids[img]]
            for i in range(len(neighbor_edges[0])):
                if neighbor_edges[0][i] != neighbor_edges[1][i]:
                    self.imgs_segment_neighbors[img][neighbor_edges[0][i]].append(neighbor_edges[1][i])
                    self.imgs_segment_neighbors[img][neighbor_edges[1][i]].append(neighbor_edges[0][i])

    # sets the foreground of the image at image_path to segments
    def set_fg_segments(self, image_path, segments):
        self.imgs_foreground_segments[image_path] = segments

    # sets the background of the image at image_path to segments
    def set_bg_segments(self, image_path, segments):
        self.imgs_background_segments[image_path] = segments

    def compute_cumulative_histograms(self):
        # for each image sum up the histograms of the chosen segments
        histograms_fg = [
            np.sum([h.flatten() for h in self.imgs_segment_histograms_hsv[img][self.imgs_foreground_segments[img]]],
                   axis=0) for img in self.images]
        histograms_bg = [
            np.sum([h.flatten() for h in self.imgs_segment_histograms_hsv[img][self.imgs_background_segments[img]]],
                   axis=0) for img in self.images]

        # combine the histograms for each image into one
        histogram_fg = np.sum(histograms_fg, axis=0)
        histogram_bg = np.sum(histograms_bg, axis=0)

        # normalize the histograms to get the final cumulative histograms
        return histogram_fg / histogram_fg.sum(), histogram_bg / histogram_bg.sum()

    # Perform graph cut using superpixels histograms
    def do_graph_cut(self, image_path, fg_hist, bg_hist, hist_comp_alg=cv2.HISTCMP_KL_DIV):
        num_nodes = len(self.imgs_segment_ids[image_path])

        # Create a graph of N nodes with an estimate of 5 edges per node
        g = maxflow.Graph[float](num_nodes, num_nodes * 5)

        # Add N nodes
        nodes = g.add_nodes(num_nodes)

        # Initialize smoothness terms: energy between neighbors
        for i in range(len(self.imgs_segment_neighbors[image_path])):
            N = self.imgs_segment_neighbors[image_path][i]  # list of neighbor superpixels
            hi = self.imgs_segment_histograms_hsv_normalized[image_path][i].flatten()  # histogram for center
            for n in N:
                if (n < 0) or (n > num_nodes):
                    continue
                # Create two edges (forwards and backwards) with capacities based on
                # histogram matching
                hn = self.imgs_segment_histograms_hsv_normalized[image_path][n].flatten()  # histogram for neighbor
                g.add_edge(nodes[i], nodes[n], 20 - cv2.compareHist(hi, hn, hist_comp_alg),
                           20 - cv2.compareHist(hn, hi, hist_comp_alg))

        # Initialize match terms: energy of assigning node to foreground or background
        for i, h in enumerate(self.imgs_segment_histograms_hsv_normalized[image_path]):
            h = h.flatten()
            energy_fg = 0
            energy_bg = 0
            if i in self.imgs_foreground_segments[image_path]:
                energy_bg = 1000  # Node is fg -> set high energy for bg
            elif i in self.imgs_background_segments[image_path]:
                energy_fg = 1000  # Node is bg -> set high energy for fg
            else:
                energy_fg = cv2.compareHist(fg_hist, h, hist_comp_alg)
                energy_bg = cv2.compareHist(bg_hist, h, hist_comp_alg)
            g.add_tedge(nodes[i], energy_fg, energy_bg)

        g.maxflow()
        return g.get_grid_segments(nodes)

    def compute_cosegmentations_graph_cut(self):
        # get cumulative BG/FG histograms, being the sum of the selected superpixel IDs normalized
        fg_hist, bg_hist = self.compute_cumulative_histograms()

        for img in self.images:
            graph_cut = self.do_graph_cut(img, fg_hist, bg_hist)

            # Get a bool mask of the pixels for a given selection of superpixel IDs
            segmentation = np.where(np.isin(self.imgs_segmentation[img], np.nonzero(graph_cut)), True, False)

            self.imgs_cosegmented[img] = np.uint8(segmentation * 255)

    def get_segment_boundaries(self, img_path):
        return find_boundaries(self.imgs_segmentation[img_path])

    # write the segmented images to specified folder
    def save_segmented_images(self, folder):
        for image in self.imgs_segmentation:
            slic.save_superpixel_image(self.imgs_float64[image], self.imgs_segmentation[image],
                                       folder + '/' + image.split('/')[-1])

    def plot_cosegmentations(self):
        for img in self.images:
            plt.subplot(1, 2, 2), plt.xticks([]), plt.yticks([])
            plt.title('segmentation')
            plt.imshow(self.imgs_cosegmented[img])
            plt.subplot(1, 2, 1), plt.xticks([]), plt.yticks([])
            superpixels = mark_boundaries(self.imgs_float64[img], self.imgs_segmentation[img])
            marking = cv2.imread('markings/' + img.split('/')[-1])
            if marking is not None:
                superpixels[marking[:, :, 0] != 255] = (1, 0, 0)
                superpixels[marking[:, :, 2] != 255] = (0, 0, 1)
            plt.imshow(superpixels)
            plt.title("Superpixels + markings")

            plt.savefig("output/segmentation/" + img.split('/')[-1], bbox_inches='tight', dpi=96)
            plt.clf()

    def compute_cosegmentations_k_means(self):
        data = []
        for img in self.images:
            for h in self.imgs_segment_histograms_hsv_normalized[img]:
                data.append(h.flatten())

        indices = np.cumsum([len(self.imgs_segment_ids[img]) for img in self.images])

        Kmean = KMeans(n_clusters=2)
        Kmean.fit(data)
        segmentation = Kmean.labels_
        for i, img in enumerate(self.images):
            self.imgs_cosegmented[img] = np.where(np.isin(alg.imgs_segmentation[img], np.nonzero(segmentation[indices[i-1]:indices[i]])), True, False)


if __name__ == '__main__':

    folder_path = '../images_icoseg/043 Christ the Redeemer-Rio de Janeiro-Leonardo Paris/'
    image_paths = [folder_path + name for name in listdir(folder_path)]

    alg = Algorithms(image_paths)

    # Segment the images into superpixels using slic and compute for each superpixel a list of its neighbors
    alg.compute_superpixels_slic(num_segments=500, compactness=20.0, max_iter=10, sigma=0)
    alg.compute_neighbors()

    alg.save_segmented_images('output/superpixel')

    # Extract features
    alg.compute_histograms_hsv(bins_H=20, bins_S=20)

    # Retrieve foreground and background segments from marking images in markings folder
    # marking images should be white with red pixels indicating foreground and blue pixels indicating background and
    # have the same name as the image they are markings for
    for image in image_paths:
        marking = cv2.imread('markings/'+image.split('/')[-1])
        if marking is not None:
            fg_segments = np.unique(alg.imgs_segmentation[image][marking[:, :, 0] != 255])
            bg_segments = np.unique(alg.imgs_segmentation[image][marking[:, :, 2] != 255])
            alg.set_fg_segments(image, fg_segments)
            alg.set_bg_segments(image, bg_segments)

    alg.compute_cosegmentations_k_means()

    #alg.compute_cosegmentations_graph_cut()

    alg.plot_cosegmentations()

    #alg.show_histogram('images/bear1.jpg')
    #alg.show_histogram('images/bear1.jpg', 1)

# TODO

# Fix show histogram overwriting
# Implement Sift and HOG into cosegmentation pipeline
# Allow for unsupervised segmentation
    # Alternatives to graph cut -> K-means

# TODO nice to have
# SLIC superpixel labels containing pixels could be more efficient with numpy

